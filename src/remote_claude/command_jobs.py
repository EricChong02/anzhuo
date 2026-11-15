"""托管长命令：独立监督进程写心跳和退出码；连接等待结果，不把启动当完成。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def read(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data['state'] == 'running' and time.time() - data['heartbeat'] > 15:
        # 无退出码且监督进程不再更新，不能把进程消失当通过；不自动重跑命令。
        data.update(state='interrupted', error='任务心跳中断，没有完整退出结果')
    return data


def worker(directory):
    os.umask(0o077)
    directory = Path(directory)
    args = json.load(sys.stdin)
    path = directory / 'state.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    proc = None
    try:
        with (directory / 'output.log').open('wb') as out:
            proc = subprocess.Popen(['/bin/zsh', '-c', args['command']], cwd=args['cwd'],
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            state.update(pid=proc.pid, heartbeat=time.time())
            save(path, state)
            deadline = time.monotonic() + args['timeout']
            while proc.poll() is None:
                if stopped or time.monotonic() >= deadline:
                    state.update(state='interrupted' if stopped else 'timeout')
                    break
                state['heartbeat'] = time.time()
                save(path, state)
                time.sleep(0.5)
            if state['state'] == 'running':
                state.update(state='completed' if proc.returncode == 0 else 'failed', exit_code=proc.returncode)
    except BaseException as exc:
        state.update(state='failed', error=str(exc)[:300])
    finally:
        if proc is not None:
            # 同组派生进程也要回收（命令不能靠 & 留下未托管的活）。
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(0.2)
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError as exc:
                if proc.poll() is None:
                    # 沙箱或系统限制可能拒绝信号；仍保存失败，不能留下 running。
                    state.update(state='interrupted', error=f'无法回收命令进程：{exc}')
                    try:
                        proc.kill()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                state.update(state='interrupted', error='命令进程未能及时回收')
        state.update(ended=time.time(), heartbeat=time.time())
        save(path, state)


class CommandJobs:
    def __init__(self, root):
        self.root = Path(root)

    def recent(self, session):
        found = []
        for path in self.root.glob('*/state.json'):
            try:
                data = read(path)
                if data['session'] == session:
                    found.append(data)
            except (OSError, ValueError, KeyError):
                continue
        return sorted(found, key=lambda x: x['started'], reverse=True)[:10]

    def status(self, session, job_id):
        if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('任务编号无效')
        directory = self.root / job_id
        data = read(directory / 'state.json')
        if data['session'] != session:
            raise ValueError('不能读取其他对话的任务')
        log = directory / 'output.log'
        data['log'] = str(log)
        if log.exists():
            with log.open('rb') as f:
                f.seek(max(0, log.stat().st_size - 3000))
                data['tail'] = f.read(3000).decode('utf-8', errors='replace')
        return data

    async def run(self, session, cwd, command, title, timeout, progress):
        job_id = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        now = time.time()
        save(directory / 'state.json', dict(job_id=job_id, session=session, title=title[:100],
             state='running', started=now, heartbeat=now, exit_code=None))
        proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), str(directory),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            proc.stdin.write(json.dumps(dict(command=command, cwd=cwd, timeout=timeout)).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            while True:
                data = self.status(session, job_id)
                progress(data)
                if data['state'] != 'running':
                    return data
                if proc.returncode is not None:
                    # 子进程可能在上次读取后刚写完终态；先重读，避免覆盖真实结果。
                    data = self.status(session, job_id)
                    if data['state'] != 'running':
                        progress(data)
                        return data
                    data.update(state='interrupted', error='监督进程退出，没有完整结果', ended=time.time())
                    data.pop('tail', None)
                    save(directory / 'state.json', data)
                    progress(data)
                    return data
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # 用户停止或连接异常都明确停止本次托管任务，不留下假运行状态。
            if proc.returncode is None:
                proc.terminate()
            await asyncio.shield(proc.wait())
            data = self.status(session, job_id)
            if data['state'] == 'running':
                data.update(state='interrupted', error='等待已取消，没有完整退出结果', ended=time.time())
                data.pop('tail', None)
                save(directory / 'state.json', data)
            progress(data)
            raise
        finally:
            if proc.returncode is not None:
                await proc.wait()


if __name__ == '__main__':
    worker(sys.argv[1])
