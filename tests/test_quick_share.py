"""手机分享的明确回执、模型路由、已有对话与账号边界。"""
import asyncio
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from anzhuo.web import Web
from anzhuo.sessions import Registry, Session


class Bridge:
    user_id = 'me'

    def __init__(self, root):
        self.registry = Registry(root / 'sessions.json')
        self.created, self.sent = [], []

    def record(self, *args, **kwargs):
        pass

    async def new_session(self, tokens, text, inc, **kwargs):
        n = self.registry.reserve_num()
        s = Session(num=n, name='手机任务', cwd='/tmp', model=kwargs.get('model'))
        self.registry.add(s)
        self.created.append((s, text, inc))

    async def submit(self, s, text, inc):
        self.sent.append((s, text, inc))


async def setup(root, prefix=''):
    w = Web('https://example.test', 0, root, web_prefix=prefix)
    w.bridge = Bridge(root)
    c = TestClient(TestServer(w.app()))
    await c.start_server()
    return w, c, f'{w.px}/q/{w._shortcut_token()}'


def form(**fields):
    data = FormData()
    for key, value in fields.items():
        data.add_field(key, value)
    data.add_field('file', b'one', filename='测试.txt', content_type='text/plain')
    return data


def test_options_are_token_protected_and_only_own_visible_latest(tmp_path):
    async def run():
        w, c, url = await setup(tmp_path)
        try:
            for n, opts in [(1, {}), (2, {'owner': 'other'}), (3, {'hidden': True}), (4, {'relay_to': 5}), (5, {})]:
                w.bridge.registry.add(Session(num=n, name=f'对话{n}', cwd='/tmp', **opts))
            assert (await c.get('/_x/q/bad/options')).status == 404
            r = await c.get(url + '/options')
            d = await r.json()
            assert r.headers['Cache-Control'] == 'no-store'
            assert len(d['targets']) == 3
            assert all('对话2' not in x and '对话3' not in x and '对话4' not in x for x in d['targets'])
            assert 'Claude Opus' in d['models']
        finally:
            await c.close()
    asyncio.run(run())


def test_multiple_files_template_explicit_model_and_receipt(tmp_path):
    async def run():
        w, c, url = await setup(tmp_path, '/window')
        try:
            data = form(text='资料里说用 Haiku', model='Claude Opus', template='总结＋待办', target='新开对话')
            data.add_field('file', b'two', filename='第二张.png', content_type='image/png')
            r = await c.post(url, data=data)
            d = await r.json()
            assert r.status == 200 and d['received'] == 2 and not d['continued']
            assert len(w.bridge.created) == 1
            s, text, inc = w.bridge.created[0]
            assert s.model == 'claude-opus-5-5'
            assert '负责人和时间' in text and '参考资料' in text
            assert len(inc.local_images) == 1
            assert sorted(p.read_bytes() for p in (tmp_path / 'inbox/web').iterdir()) == [b'one', b'two']
            assert d['url'] == f'https://example.test/window/_x/share/{s.num}'
            assert d['model'] == 'Opus 5.5' and '处理完成' in d['message']
            assert w._shortcut_token() not in d['url']
        finally:
            await c.close()
    asyncio.run(run())


def test_continue_follows_relay_keeps_model_and_rejects_other_accounts(tmp_path):
    async def run():
        w, c, url = await setup(tmp_path)
        try:
            for n, opts in [(1, {'relay_to': 2}), (2, {'model': 'claude-sonnet-5-5'}), (3, {'owner': 'other'}), (4, {'hidden': True})]:
                w.bridge.registry.add(Session(num=n, name='已有', cwd='/tmp', **opts))
            d = await (await c.post(url, data=form(text='新资料', target='#1 · 已有', model='GPT'))).json()
            assert d['continued'] and d['num'] == 2 and '沿用原对话' in d['message']
            assert d['model'] == 'Sonnet 5.5' and not w.bridge.created
            assert len(w.bridge.sent) == 1 and w.bridge.sent[0][0].num == 2
            baseline = set((tmp_path / 'inbox/web').iterdir())
            for target in ['#3 · 已有', '#4 · 已有', '#999 · 不存在', 'bad']:
                r = await c.post(url, data=form(text='不要发送', target=target))
                assert r.status in {400, 404} and not (await r.json())['ok']
                assert set((tmp_path / 'inbox/web').iterdir()) == baseline
            assert len(w.bridge.sent) == 1
        finally:
            await c.close()
    asyncio.run(run())


def test_voice_text_and_bad_options_do_not_create_tasks_or_leave_files(tmp_path):
    async def run():
        w, c, url = await setup(tmp_path)
        try:
            d = await (await c.post(url, data={'text': '口述的任务', 'model': 'haiku'})).json()
            assert d['received'] == 0 and d['ok']
            assert w.bridge.created[0][1] == '口述的任务'
            for fields in [{'model': 'no-model'}, {'template': 'bad-template'}, {'target': 'bad-target'}]:
                r = await c.post(url, data=form(text='测试', **fields))
                assert r.status == 400 and not (await r.json())['ok']
                assert not list((tmp_path / 'inbox/web').iterdir())
            r = await c.post(url, data={'text': ' ', 'template': '总结＋待办'})
            assert r.status == 400
            assert len(w.bridge.created) == 1
        finally:
            await c.close()
    asyncio.run(run())


def test_open_link_requires_login_and_restores_target_after_login(tmp_path):
    async def run():
        from anzhuo.web.common import hash_password
        w = Web('http://example.test', 0, tmp_path, user='admin', pw_hash=hash_password('test', iters=1000))
        w.bridge = Bridge(tmp_path)
        w.bridge.registry.add(Session(num=8, name='已有', cwd='/tmp'))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            r = await c.get('/_x/share/8', allow_redirects=False)
            assert r.status == 302 and 'next=/_x/share/8' in r.headers['Location']
            await c.post('/_x/login', data={'username': 'admin', 'password': 'test'}, allow_redirects=False)
            r = await c.get('/_x/share/8', allow_redirects=False)
            assert r.status == 302 and r.headers['Location'] == '/_x/#s8'
        finally:
            await c.close()
    asyncio.run(run())
