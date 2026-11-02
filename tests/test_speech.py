"""朗读的正文、缓存生命周期、生成去重和登录后播放链路。"""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

from aiohttp.test_utils import TestClient, TestServer

from anzhuo.report import Turn
from anzhuo.sessions import Session
from anzhuo.speech import Speech, chunks, readable_text
from anzhuo.web import Web, hash_password


def test_readable_reply_preserves_prose_and_skips_machine_noise():
    text = readable_text('# 结论\n**先备份**，再修改。[说明](https://example.com/a)\n```sh\nrm -rf secret\n```\n路径 `/Users/a/data.txt`。\n|项目|结果|\n|---|---|\n|测试|通过|')
    assert '先备份，再修改。说明' in text and '测试，通过' in text
    assert 'rm -rf' not in text and 'https://' not in text and '/Users' not in text
    assert '代码段请查看原文' in text
    assert not readable_text('  \n')
    assert chunks('第一句。第二句！第三句。') == ['第一句。', '第二句！第三句。']
    assert ''.join(chunks('长' * 1000)) == '长' * 1000
    assert all(len(part) <= 80 for part in chunks('长' * 1000))


def cached(s, name, *, num=1, ago=0, state='ready', size=10, protected=0):
    key = name * 64
    s._save(key, dict(key=key, num=num, state=state, accessed=time.time()-ago,
                      protected_until=protected, duration=30, parts=1, error=''))
    (s.directory(key) / 'audio.mp3').write_bytes(b'x' * size)
    return key


def test_cache_expiration_touch_and_active_protection(tmp_path):
    s = Speech(tmp_path)
    old = cached(s, 'a', ago=8*86400)
    touched = cached(s, 'b', ago=8*86400)
    playing = cached(s, 'c', ago=8*86400, protected=time.time()+500)
    reader = cached(s, 'd', ago=8*86400)
    s.readers[reader] = 1
    generating = cached(s, 'e', ago=8*86400, state='generating')
    s.jobs[generating] = object()
    s.touch(touched)
    assert s.cleanup()
    assert not s.directory(old).exists()
    assert all(s.directory(k).exists() for k in [touched, playing, reader, generating])
    s.touch(playing, playing=False)
    s.readers.clear(); s.jobs.clear()
    # 停止播放也更新最近访问时间；另外两个失去保护的过期项会清理。
    s.cleanup()
    assert s.directory(playing).exists() and not s.directory(reader).exists() and not s.directory(generating).exists()


def test_cache_lru_limit_and_no_eviction_of_playing(tmp_path):
    s = Speech(tmp_path, max_bytes=1200)
    oldest = cached(s, 'a', ago=100, size=600)
    newest = cached(s, 'b', ago=10, size=600)
    s.cleanup()
    assert not s.directory(oldest).exists() and s.directory(newest).exists()
    s.touch(newest, playing=True)
    hold = s.meta(newest)['protected_until']
    s.touch(newest, playing=False)
    assert s.meta(newest)['protected_until'] == hold
    assert not s.cleanup(reserve=1000)
    assert s.directory(newest).exists()
    assert s.status(newest)['state'] == 'ready'


def test_interrupted_generation_is_retryable_and_invalid_keys_rejected(tmp_path):
    s = Speech(tmp_path)
    key = cached(s, 'a', state='generating')
    assert s.status(key)['state'] == 'error'
    try:
        s.directory('../../elsewhere')
        assert False
    except ValueError:
        pass


def test_generation_deduplicates_and_deleting_a_session_cancels_only_its_jobs(tmp_path, monkeypatch):
    async def go():
        s = Speech(tmp_path, python=Path(__file__))
        began, release = asyncio.Event(), asyncio.Event()
        async def generate(key, parts):
            began.set(); await release.wait()
        monkeypatch.setattr(s, '_generate', generate)
        a = await s.start(1, '你好。')
        await began.wait()
        b = await s.start(1, '你好。')
        assert a['key'] == b['key'] and len(s.jobs) == 1
        other = cached(s, 'f', num=2)
        await s.remove_session(1)
        assert not s.directory(a['key']).exists() and s.directory(other).exists()
        assert not s.jobs
        await s.close()
    asyncio.run(go())


def test_authenticated_generation_stream_download_range_and_delete(tmp_path, monkeypatch):
    async def go():
        w = Web('http://x.example', 0, tmp_path, user='u', pw_hash=hash_password('p', iters=1000))
        s = Session(num=6, name='朗读', cwd=str(tmp_path))
        w.bridge = NS(registry=NS(get=lambda n: s if n == 6 else None, save=lambda: None, current=6),
                      runner=NS(turns={}, busy=lambda n: False))
        tid = w.save_turn('#6 朗读', Turn(started=100, ended=120, final_text='这是**真实回复**。'), str(tmp_path), '完成')
        w.speech.python = Path(__file__)
        first, finish = asyncio.Event(), asyncio.Event()
        async def generate(key, parts):
            assert parts == ['这是真实回复。']
            m = w.speech.meta(key); m.update(state='generating', parts=1, duration=1)
            w.speech._save(key,m)
            p = w.speech.directory(key) / 'audio.part'
            p.write_bytes(b'first-'); first.set()
            await finish.wait()
            with p.open('ab') as f: f.write(b'last')
            p.replace(p.with_name('audio.mp3'))
            m = w.speech.meta(key); m.update(state='ready',duration=2,parts=2)
            w.speech._save(key,m)
        monkeypatch.setattr(w.speech,'_generate',generate)
        c = TestClient(TestServer(w.app())); await c.start_server()
        try:
            url='/_x/api/speech/6'
            assert (await c.post(url,json={'tid':tid})).status == 401
            await c.post('/_x/login', data={'username':'u','password':'p'})
            w.base = 'https://x.example'  # 测试端口是 HTTP；登录后用生产 Origin 规则核验。
            assert (await c.post(url,json={'tid':tid},headers={'Origin':'https://other.example'})).status == 403
            assert (await c.post(url,json={'tid':'9-123'})).status == 404
            assert not (await (await c.post(url,json={'text':'注入正文'})).json())['ok']
            d=await (await c.post(url,json={'tid':tid})).json(); assert d['ok']; key=d['key']
            await first.wait()
            stream=await c.get('/_x/audio/'+key+'.mp3')
            assert await asyncio.wait_for(stream.content.readexactly(6),2) == b'first-'
            assert not (await c.get('/_x/audio/'+key+'.mp3?download=1')).status == 200
            finish.set()
            assert await stream.read() == b'last'
            status=await (await c.get('/_x/api/audio/'+key)).json()
            assert status['state']=='ready' and status['parts']==2
            r=await c.get('/_x/audio/'+key+'.mp3',headers={'Range':'bytes=0-4'})
            assert r.status==206 and await r.read()==b'first'
            r=await c.get('/_x/audio/'+key+'.mp3?download=1')
            assert await r.read()==b'first-last' and 'attachment' in r.headers['Content-Disposition']
            assert (await c.post('/_x/api/audio/'+key,json={'playing':False},headers={'Origin':'https://other.example'})).status==403
            assert (await c.post('/_x/api/manage/6',json={'action':'delete'})).status == 200
            assert not w.speech.directory(key).exists()
            assert (await c.get('/_x/audio/'+key+'.mp3')).status==404
        finally:
            finish.set(); await c.close()
    asyncio.run(go())


def test_worker_completion_extends_lock_screen_protection_and_failure_is_retryable(tmp_path, monkeypatch):
    async def go():
        s = Speech(tmp_path, python=Path(__file__))
        released, submitted = asyncio.Event(), asyncio.Event()
        calls = 0
        class Input:
            def write(self, raw):
                req=json.loads(raw)
                (Path(req['out'])/'audio.part').write_bytes(b'mp3')
            async def drain(self):
                submitted.set()
        class Proc:
            stdin=Input()
        async def worker(): return Proc()
        async def event(proc):
            nonlocal calls
            await released.wait(); calls+=1
            return {'duration':2000,'parts':1} if calls==1 else {'done':True}
        monkeypatch.setattr(s,'_worker',worker)
        monkeypatch.setattr(s,'_event',event)
        r=await s.start(1,'锁屏保护测试。'); key=r['key']
        await submitted.wait()
        s.touch(key,playing=True)
        released.set()
        await asyncio.gather(*s.jobs.values())
        assert s.status(key)['state']=='ready'
        assert s.meta(key)['protected_until']>time.time()+3900
        # 生成失败不能留下貌似完整的半截音频；相同回复可再次生成。
        async def bad_event(proc): raise OSError('test')
        monkeypatch.setattr(s,'_event',bad_event)
        r=await s.start(2,'故障重试测试。'); failed=r['key']
        await asyncio.gather(*s.jobs.values())
        assert s.status(failed)['state']=='error'
        assert not (s.directory(failed)/'audio.part').exists()
        calls=0; monkeypatch.setattr(s,'_event',event)
        r=await s.start(2,'故障重试测试。')
        await asyncio.gather(*s.jobs.values())
        assert r['key']==failed and s.status(failed)['state']=='ready'
        await s.close()
    asyncio.run(go())


def test_kokoro_phoneme_budget_preserves_every_character():
    from anzhuo.speech_worker import kokoro_parts
    text = '这是长段落，必须完整念完。' * 50
    parts = kokoro_parts(text, lambda s: s * 6)
    assert ''.join(parts) == text
    assert all(len(p) * 6 <= 480 for p in parts)
    assert all(p for p in parts)
    # 极端异常必须报错，不能静默截掉内容。
    try:
        kokoro_parts('单', lambda s: 'x' * 600)
        assert False
    except ValueError:
        pass


def test_default_voice_uses_chinese_vivian_streaming():
    from anzhuo.speech_worker import MODEL, generate_parts
    calls = []
    class Model:
        def generate_custom_voice(self, **kw):
            calls.append(kw)
            yield 'audio'
    assert 'Qwen3-TTS' in MODEL
    assert list(generate_parts(Model(), ['第一句。', '第二句。'])) == ['audio', 'audio']
    assert all(c['speaker'] == 'Serena' and c['language'] == 'Chinese' and c['stream'] for c in calls)


def test_releasing_player_cancels_only_unused_generation_and_unblocks_queue(tmp_path, monkeypatch):
    async def go():
        s = Speech(tmp_path, python=Path(__file__))
        began, second = asyncio.Event(), asyncio.Event()
        async def generate(key, parts):
            async with s.lock:
                m=s.meta(key);m['state']='generating';s._save(key,m)
                (began if parts==['旧回复。'] else second).set()
                await asyncio.Event().wait()
        monkeypatch.setattr(s,'_generate',generate)
        a=await s.start(1,'旧回复。',listener='player-a')
        await began.wait()
        # 另一位听众使用同一份音频时，不得被前一位的关闭操作打断。
        await s.start(1,'旧回复。',listener='player-b')
        await s.release('player-a')
        assert a['key'] in s.jobs
        b=await s.start(2,'新回复。',listener='player-c')
        assert s.status(b['key'])['queue_ahead']==1
        await s.release('player-b')
        await asyncio.wait_for(second.wait(),1)
        assert a['key'] not in s.jobs and b['key'] in s.jobs
        await s.release('player-c')
        assert not s.jobs
        await s.close()
    asyncio.run(go())


def test_close_before_start_arrives_does_not_leave_orphan_job(tmp_path):
    async def go():
        s=Speech(tmp_path,python=Path(__file__))
        await s.release('old-request')
        try:
            await s.start(1,'这条不应再生成。',listener='old-request')
            assert False
        except ValueError:
            pass
        assert not s.jobs
        await s.close()
    asyncio.run(go())


def test_lost_player_expires_but_active_stream_is_retained(tmp_path, monkeypatch):
    async def go():
        s=Speech(tmp_path,python=Path(__file__))
        async def generate(*args): await asyncio.Event().wait()
        monkeypatch.setattr(s,'_generate',generate)
        r=await s.start(1,'失联测试。',listener='gone')
        key=r['key'];s.listeners['gone']=(key,time.time()-121)
        s.readers[key]=1
        await s.expire_listeners()
        assert key in s.jobs
        s.readers.clear();await s.expire_listeners()
        assert key not in s.jobs and 'gone' not in s.listeners
        await s.close()
    asyncio.run(go())


def test_release_api_is_authenticated_origin_checked_and_bound_to_request(tmp_path,monkeypatch):
    async def go():
        w=Web('http://x.example', 0, tmp_path, user='u',pw_hash=hash_password('p',iters=1000))
        session=Session(num=1,name='释放测试',cwd=str(tmp_path))
        w.bridge=NS(registry=NS(get=lambda n:session))
        tid=w.save_turn('#1 测试',Turn(started=100,ended=120,final_text='正在等待的回复。'),'/tmp','完成')
        w.speech.python=Path(__file__)
        async def generate(*args): await asyncio.Event().wait()
        monkeypatch.setattr(w.speech,'_generate',generate)
        c=TestClient(TestServer(w.app()));await c.start_server()
        try:
            url='/_x/api/speech/release'
            assert (await c.post(url,json={'listener':'one'})).status==401
            await c.post('/_x/login',data={'username':'u','password':'p'})
            w.base='https://x.example'
            r=await (await c.post('/_x/api/speech/1',json={'tid':tid,'listener':'one'})).json()
            key=r['key']
            assert (await c.post(url,json={'listener':'one'},headers={'Origin':'https://bad.example'})).status==403
            await c.post(url,json={'listener':'other'})
            assert key in w.speech.jobs
            assert (await c.post(url,json={'listener':'one'})).status==200
            assert key not in w.speech.jobs
            assert w.speech.status(key)['state']=='error'
        finally: await c.close()
    asyncio.run(go())
