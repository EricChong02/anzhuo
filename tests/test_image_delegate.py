import asyncio
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from anzhuo import image_delegate as d
from anzhuo.report import Turn
from anzhuo.sessions import Runner, Session

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class Conn:
    mode = 'success'
    instances = []

    def __init__(self, cwd, event, approve):
        self.cwd, self.event, self.approve = Path(cwd), event, approve
        self.done = asyncio.Event()
        self.turn_status, self.turn_error = '', ''
        self.interrupted = self.closed = False
        self.instances.append(self)

    async def start(self):
        pass

    async def open_thread(self, tid, instructions):
        assert tid is None and 'image_gen' in instructions
        return 'dedicated-image-thread'

    async def start_turn(self, text, images, effort):
        self.references = images
        self.event('item/started', {'item': {'type': 'imageGeneration'}})
        if self.mode == 'wait':
            return 'turn'
        if self.mode != 'missing':
            target = self.cwd / 'final.png'
            target.write_bytes(PNG if self.mode != 'fake' else b'not an image')
            if self.mode == 'outside':
                target = self.cwd.parent / 'outside.png'
                target.write_bytes(PNG)
            (self.cwd / 'result.json').write_text(json.dumps({'images': [str(target)]}))
        self.turn_status = 'completed'
        self.done.set()
        return 'turn'

    async def interrupt(self):
        self.interrupted = True

    async def close(self):
        self.closed = True


def options(tmp_path, progress):
    return dict(prompt='画一张图', references=[], cwd=str(tmp_path), output_dir='', add_dirs=[], parent_num=3,
                progress=lambda *args: progress.append(args), approve=lambda *args: None)


@pytest.mark.parametrize('mode,state', [('success', 'completed'), ('missing', 'failed'), ('outside', 'failed'), ('fake', 'failed')])
def test_delegate_requires_real_output(tmp_path, monkeypatch, mode, state):
    monkeypatch.setattr(d, 'CodexConn', Conn)
    monkeypatch.setattr(Conn, 'mode', mode)

    async def go():
        progress = []
        runner = d.ImageDelegator(tmp_path / 'records')
        result = await runner.run(**options(tmp_path, progress))
        assert result['state'] == state
        rec = json.loads((tmp_path / 'records' / (result['job_id'] + '.json')).read_text())
        assert rec['state'] == state and rec['thread_id'] == 'dedicated-image-thread'
        assert 'prompt' not in rec
        assert Conn.instances[-1].closed
        assert progress[0][1] == 'queued' and progress[-1][1] == state
        if state == 'completed':
            assert Path(result['images'][0]).read_bytes() == PNG
        else:
            assert result['images'] == []
    asyncio.run(go())


def test_delegate_reference_and_output_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'CodexConn', Conn)
    monkeypatch.setattr(Conn, 'mode', 'success')
    ref = tmp_path / 'ref.png'
    ref.write_bytes(PNG)

    async def go():
        runner = d.ImageDelegator(tmp_path / 'records')
        opts = options(tmp_path, [])
        opts['references'] = [str(ref)]
        result = await runner.run(**opts)
        assert result['state'] == 'completed' and Conn.instances[-1].references == [ref.resolve()]
        opts['output_dir'] = str(tmp_path.parent / 'unapproved')
        with pytest.raises(ValueError, match='输出目录'):
            await runner.run(**opts)
        opts['output_dir'] = ''
        opts['references'] = [str(tmp_path / 'missing.png')]
        with pytest.raises(ValueError, match='参考图'):
            await runner.run(**opts)
    asyncio.run(go())


def test_delegate_timeout_and_cancel_release_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'CodexConn', Conn)
    monkeypatch.setattr(Conn, 'mode', 'wait')

    async def go():
        runner = d.ImageDelegator(tmp_path / 'records')
        result = await runner.run(**options(tmp_path, []), timeout=.02)
        assert result['state'] == 'timeout' and Conn.instances[-1].interrupted and Conn.instances[-1].closed
        task = asyncio.create_task(runner.run(**options(tmp_path, [])))
        while not runner.sem.locked():
            await asyncio.sleep(.001)
        queued = asyncio.create_task(runner.run(**options(tmp_path, [])))
        await asyncio.sleep(.01)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runner.sem.locked() and Conn.instances[-1].closed
        rows = [json.loads(p.read_text()) for p in (tmp_path / 'records').glob('*.json')]
        assert sorted(r['state'] for r in rows) == ['cancelled', 'cancelled', 'timeout']
    asyncio.run(go())


def test_parent_attachment_progress_and_stop(tmp_path):
    async def go():
        runner = Runner(NS(path=tmp_path / 'sessions.json'), None)
        s = Session(3, '任务', str(tmp_path))
        turn = Turn(started=time.time())
        image = tmp_path / 'result.png'
        image.write_bytes(PNG)

        approvals = []
        async def deny(*args):
            approvals.append(args[-1])
            return False
        runner.decide = deny
        async def generate(**opts):
            assert await opts['approve']('execCommandApproval', {'command': 'git push'}) == {'decision': 'denied'}
            opts['progress']('job', 'running', '处理中')
            opts['progress']('job', 'completed', '已生成')
            return dict(job_id='job', state='completed', images=[str(image)])
        runner.image_delegate = NS(run=generate)
        result = await runner._generate_images(s, turn, {'prompt': '画图'})
        assert result['state'] == 'completed' and turn.images == [str(image)]
        assert turn.agents['image:job']['status'] == 'completed'
        assert '用户已收到' in result['message'] and not runner.image_tasks
        assert approvals == ['git push']

        async def waiting(**opts):
            await asyncio.Event().wait()
        runner.image_delegate = NS(run=waiting)
        runner.turns[3] = turn
        task = asyncio.create_task(runner._generate_images(s, turn, {'prompt': '画图'}))
        while not runner.image_tasks.get(3):
            await asyncio.sleep(.001)
        assert await runner.stop(3)
        assert (await task)['state'] == 'cancelled' and not runner.image_tasks
    asyncio.run(go())


def test_restart_marks_unfinished_without_resubmitting(tmp_path):
    records = tmp_path / 'records'
    records.mkdir()
    (records / 'job.json').write_text(json.dumps(dict(job_id='job', state='running')))
    d.ImageDelegator(records)
    assert json.loads((records / 'job.json').read_text())['state'] == 'interrupted'


def test_claude_image_prompt_includes_reference_paths(tmp_path):
    ref = tmp_path / 'input.png'
    ref.write_bytes(PNG)
    async def go():
        from anzhuo.sessions import Job
        runner = Runner(None, None)
        rows = [r async for r in runner._prompt(Job(text='修改这张照片', images=[ref]))]
        content = rows[0]['message']['content']
        assert content[0]['type'] == 'image'
        assert str(ref.resolve()) in content[1]['text']
        assert content[-1]['text'] == '修改这张照片'
    asyncio.run(go())
