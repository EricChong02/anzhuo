import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace as NS

import pytest

from anzhuo.command_jobs import CommandJobs, read, save
from anzhuo.engines import claude
from anzhuo.report import Turn, live_agents
from anzhuo.sessions import Runner, Session


@pytest.mark.parametrize('command,state,code', [('echo hello', 'completed', 0), ('echo broken; exit 7', 'failed', 7)])
def test_real_exit_and_reconnect(tmp_path, command, state, code):
    async def go():
        seen = []
        jobs = CommandJobs(tmp_path / 'jobs')
        result = await jobs.run(1, str(tmp_path), command, '验收', 5, seen.append)
        assert result['state'] == state and result['exit_code'] == code
        assert seen[0]['state'] == 'running' and seen[-1]['state'] == state
        # 重建管理器仍能读到退出码；元数据不保存命令或输出内容。
        got = CommandJobs(jobs.root).status(1, result['job_id'])
        assert got['state'] == state and got['tail'].strip()
        assert jobs.recent(1)[0]['job_id'] == result['job_id'] and not jobs.recent(2)
        raw = json.loads((jobs.root / result['job_id'] / 'state.json').read_text())
        assert 'command' not in raw and 'tail' not in raw
        with pytest.raises(ValueError):
            jobs.status(2, result['job_id'])
        with pytest.raises(ValueError):
            jobs.status(1, '../state')
    asyncio.run(go())


def test_timeout_and_cancel_reap_child(tmp_path):
    async def go():
        jobs, seen = CommandJobs(tmp_path / 'jobs'), []
        result = await jobs.run(1, str(tmp_path), 'sleep 60', '超时', 1, seen.append)
        assert result['state'] == 'timeout' and result['exit_code'] is None
        seen.clear()
        task = asyncio.create_task(jobs.run(1, str(tmp_path), 'sleep 60', '停止', 60, seen.append))
        while not seen or 'pid' not in seen[-1]:
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen[-1]['state'] == 'interrupted'
        import os
        with pytest.raises(ProcessLookupError):
            os.kill(seen[-1]['pid'], 0)
    asyncio.run(go())


def test_stale_heartbeat_is_not_success(tmp_path):
    p = tmp_path / 'state.json'
    save(p, dict(state='running', heartbeat=time.time() - 30, exit_code=None))
    assert read(p)['state'] == 'interrupted' and read(p)['exit_code'] is None


def test_caller_exit_does_not_kill_supervised_job(tmp_path):
    import subprocess
    import sys
    script = '''
import asyncio, os, sys
from anzhuo.command_jobs import CommandJobs
async def main():
    def progress(data):
        if data.get('pid'):
            os._exit(0)
    await CommandJobs(sys.argv[1]).run(1, sys.argv[2], 'sleep 2; echo survived', '独立运行', 10, progress)
asyncio.run(main())
'''
    subprocess.run([sys.executable, '-c', script, str(tmp_path / 'jobs'), str(tmp_path)], check=True, timeout=10)
    state = next((tmp_path / 'jobs').glob('*/state.json'))
    deadline = time.monotonic() + 10
    while read(state)['state'] == 'running' and time.monotonic() < deadline:
        time.sleep(0.1)
    assert read(state)['state'] == 'completed'
    assert (state.parent / 'output.log').read_text().strip() == 'survived'


def tools(tmp_path, monkeypatch, restricted=False):
    monkeypatch.setattr(claude, 'create_sdk_mcp_server', lambda name, tools: {t.name: t.handler for t in tools})
    r = Runner(NS(path=tmp_path / 'sessions.json'), None)
    s = Session(1, '验收', str(tmp_path), root=str(tmp_path) if restricted else '')
    t = Turn(started=time.time())
    return r, s, t, claude.ClaudeEngine.tools(r, t, s)


def test_tool_approval_and_real_progress(tmp_path, monkeypatch):
    r, s, t, ts = tools(tmp_path, monkeypatch)
    calls = []

    async def approve(*args):
        calls.append(args[-1])
        return args[-1] != 'echo denied'
    monkeypatch.setattr(claude.policy, 'approve', approve)

    async def go():
        denied = await ts['run_command'](dict(command='echo denied', title='拒绝'))
        assert denied['isError'] and not (tmp_path / 'command_jobs').exists()
        result = await ts['run_command'](dict(command='echo checked', title='真实验收'))
        assert not result['isError']
        assert calls == ['echo denied', 'echo checked']
        assert live_agents(t)[0]['status'] == 'done'
        assert t.timeline[-1]['text'] == '真实验收：命令通过'
        assert t.commands == ['echo checked']
    asyncio.run(go())


def test_restricted_and_role_cannot_bypass(tmp_path, monkeypatch):
    r, s, t, ts = tools(tmp_path, monkeypatch, True)
    assert 'run_command' not in ts and 'command_status' not in ts
    assert 'run_command' not in claude.build_user_instructions(str(tmp_path))
    r, s, t, ts = tools(tmp_path, monkeypatch)
    r.role = lambda s: {'disallowed': ['Bash']}
    result = asyncio.run(ts['run_command'](dict(command='echo no', title='无权限')))
    assert result['isError'] and not (tmp_path / 'command_jobs').exists()


def test_background_bash_blocked_before_execution(tmp_path, monkeypatch):
    r, s, t, _ = tools(tmp_path, monkeypatch)
    hook = claude.ClaudeEngine.guard(r, s, t)
    result = asyncio.run(hook(dict(tool_input=dict(command='sleep 60', run_in_background=True)), '', None))
    assert result['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_phone_stop_cancels_managed_command(tmp_path, monkeypatch):
    r, s, t, ts = tools(tmp_path, monkeypatch)
    async def go():
        r.turns[s.num] = t
        task = asyncio.create_task(ts['run_command'](dict(command='sleep 60', title='停止联调')))
        jobs = CommandJobs(tmp_path / 'command_jobs')
        while not jobs.recent(1) or 'pid' not in jobs.recent(1)[0]:
            await asyncio.sleep(0.05)
        assert await r.stop(s.num)
        result = await task
        assert result['isError'] and t.interrupted
        assert jobs.recent(1)[0]['state'] == 'interrupted'
        assert not r.image_tasks and live_agents(t)[0]['status'] == 'failed'
    asyncio.run(go())


def test_live_command_list_only_shows_running_commands():
    import subprocess
    source = (Path(claude.__file__).parents[1] / 'static/app/core.js').read_text()
    function = source[source.index('function agentsHTML('):source.index('function procHTML(')]
    program = 'const esc=s=>s;const dur=s=>s;' + function
    program += """
const finished = ['done', 'failed', 'stopped'].map((status, i) =>
  ({name: '结束命令' + i, kind: '命令', status, secs: 3}));
const running = {name: '进行中的命令', kind: '命令', status: 'running', secs: 2};
console.log(agentsHTML(finished) || '空列表');
console.log(agentsHTML([...finished, running], true));
"""
    empty, html = subprocess.check_output(['node', '-e', program], text=True).splitlines()
    assert empty == '空列表'
    assert '1 条后台命令在运行' in html and '进行中的命令' in html
    assert '结束命令' not in html and '含已结束任务' not in html
