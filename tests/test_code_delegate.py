import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from anzhuo import code_delegate as d
from anzhuo.report import Turn
from anzhuo.sessions import Runner, Session


def args(**kw):
    return dict(kind='backend', task='实现批量统计函数并保留输入校验', why='有清楚接口和独立测试，可与前端分开实现', paths=['stats.py', 'test_stats.py'], acceptance='支持空输入、负数和浮点，给出单元测试', **kw)


class Conn:
    mode = 'ok'
    instances = []
    def __init__(self, cwd, event, approve):
        self.cwd, self.event = Path(cwd), event
        self.done = asyncio.Event()
        self.turn_status, self.turn_error = '', ''
        self.closed = self.interrupted = False
        self.instances.append(self)
    async def start(self): pass
    async def open_thread(self, tid, instructions): return tid or 'worker-thread'
    async def start_turn(self, text, images, effort):
        if self.mode == 'wait': return
        (self.cwd / ('outside.py' if self.mode == 'outside' else 'stats.py')).write_text('def total(xs): return sum(xs)\n')
        (self.cwd / 'test_stats.py').write_text('from stats import total\ndef test_total(): assert total([1, 2]) == 3\n')
        self.event('item/completed', {'item': {'type': 'agentMessage', 'text': '已实现；需要父任务核验'}})
        self.turn_status = 'completed'
        self.done.set()
    async def close(self): self.closed = True
    async def interrupt(self): self.interrupted = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'stats.py').write_text('# 尚未实现\n')
    (source / '.gitignore').write_text('secret.txt\n')
    (source / 'secret.txt').write_text('不应复制')
    async def init():
        await d.git(source, 'init', '-q')
        await d.git(source, 'add', '--all')
    asyncio.run(init())
    async def limits(): return {'five_hour': (.2, None), 'seven_day': (.3, None)}
    monkeypatch.setattr(d, 'rate_limits', limits)
    monkeypatch.setattr(d, 'WorkerConn', Conn)
    monkeypatch.setattr(Conn, 'mode', 'ok')
    return source, d.CodeDelegator(tmp_path / 'jobs')


def test_patch_isolated_and_bounded_review(setup):
    source, delegator = setup
    async def go():
        row = await delegator.run(cwd=str(source), parent_num=1, args=args(), progress=lambda *a: None)
        assert row['state'] == 'completed'
        assert (source / 'stats.py').read_text() == '# 尚未实现\n'
        assert not (Path(row['work']) / 'secret.txt').exists()
        assert 'def total' in Path(row['patch']).read_text()
        assert set(row['files']) == {'stats.py', 'test_stats.py'}
        await d.git(source, 'apply', '--check', row['patch'])
        for i in range(3):
            row = await delegator.run(cwd=str(source), parent_num=1, args=args(job_id=row['job_id']), progress=lambda *a: None)
            assert row['state'] == 'completed' and row['rounds'] == i + 2
        with pytest.raises(ValueError, match='三轮'):
            await delegator.run(cwd=str(source), parent_num=1, args=args(job_id=row['job_id']), progress=lambda *a: None)
        with pytest.raises(ValueError, match='其他对话'):
            await delegator.run(cwd=str(source), parent_num=2, args=args(job_id=row['job_id']), progress=lambda *a: None)
    asyncio.run(go())


@pytest.mark.parametrize('limits', [{}, {'five_hour': (.9, None)}, {'seven_day': (1, None)}])
def test_quota_unknown_or_tight_skips(setup, monkeypatch, limits):
    source, delegator = setup
    async def read(): return limits
    monkeypatch.setattr(d, 'rate_limits', read)
    async def go():
        row = await delegator.run(cwd=str(source), parent_num=1, args=args(), progress=lambda *a: None)
        assert row['state'] == 'skipped' and not (delegator.records / row['job_id'] / 'work').exists()
    asyncio.run(go())


def test_outside_scope_and_timeout(setup, monkeypatch):
    source, delegator = setup
    async def go():
        monkeypatch.setattr(Conn, 'mode', 'outside')
        row = await delegator.run(cwd=str(source), parent_num=1, args=args(), progress=lambda *a: None)
        assert row['state'] == 'failed' and '范围' in row['error'] and 'patch' not in row
        monkeypatch.setattr(Conn, 'mode', 'wait')
        row = await delegator.run(cwd=str(source), parent_num=1, args=args(), progress=lambda *a: None, timeout=.2)
        assert row['state'] == 'timeout' and Conn.instances[-1].closed
        task = asyncio.create_task(delegator.run(cwd=str(source), parent_num=1, args=args(), progress=lambda *a: None))
        await asyncio.sleep(.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not delegator.sem.locked()
    asyncio.run(go())


def test_runner_blocks_fenced_and_stopped(tmp_path):
    async def go():
        r = Runner(NS(path=tmp_path / 'sessions.json'), None)
        for session, turn in [(Session(1, '受限', str(tmp_path), root=str(tmp_path)), Turn(started=0)),
                              (Session(2, '停止', str(tmp_path)), Turn(started=0, interrupted=True))]:
            result = await r._delegate_code(session, turn, args())
            assert result['state'] == 'skipped' and not hasattr(r, 'code_delegate')
    asyncio.run(go())


def test_worker_sandbox_settings():
    async def go():
        c = d.WorkerConn('/tmp/isolated', lambda *a: None)
        seen = []
        async def request(method, params):
            seen.append(params)
            return {'thread': {'id': 'dedicated'}}
        c.request = request
        await c.open_thread(None, '规则')
        assert seen[0]['sandbox'] == 'workspace-write'
        assert not seen[0]['config']['sandbox_workspace_write']['network_access']
        assert seen[0]['model'] == 'gpt-6.1-sol'
    asyncio.run(go())


def test_parent_stop_cancels_worker(tmp_path):
    async def go():
        from anzhuo.sessions import Registry
        registry = Registry(tmp_path / 'sessions.json')
        s = registry.add(Session(1, '代码', str(tmp_path)))
        r = Runner(registry, None)
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def run(**kw):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        r.code_delegate = NS(run=run)
        turn = Turn(started=0)
        r.turns[s.num] = turn
        task = asyncio.create_task(r._delegate_code(s, turn, args()))
        await entered.wait()
        await r.stop(s.num)
        assert (await task)['state'] == 'cancelled'
        assert stopped.is_set() and not r.image_tasks
    asyncio.run(go())


def test_sdk_exposes_only_unfenced_tool(tmp_path, monkeypatch):
    from anzhuo.engines import claude
    monkeypatch.setattr(claude, 'create_sdk_mcp_server', lambda name, tools: tools)
    r = Runner(NS(path=tmp_path / 'sessions.json'), None)
    normal = claude.ClaudeEngine.tools(r, Turn(started=0), Session(1, '普通', str(tmp_path)))
    restricted = claude.ClaudeEngine.tools(r, Turn(started=0), Session(2, '受限', str(tmp_path), root=str(tmp_path)))
    assert 'delegate_code' in {t.name for t in normal}
    assert 'delegate_code' not in {t.name for t in restricted}
    assert 'delegate_code' not in claude.build_user_instructions(str(tmp_path))


def test_snapshot_keeps_tracked_ignored_file(setup):
    source, delegator = setup
    async def go():
        await d.git(source, 'add', '--force', 'secret.txt')
        target = source.parent / 'copy'
        await delegator.snapshot(source, target)
        tracked = (await d.git(target, 'ls-files')).decode()
        assert 'secret.txt' in tracked
    asyncio.run(go())
