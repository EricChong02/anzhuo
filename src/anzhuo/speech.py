"""按需朗读：本机独立模型进程、渐进 MP3、七天 / 1GiB 缓存。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

MAX_BYTES = 1024 ** 3
MAX_AUDIO = 128 * 1024 ** 2
KEEP = 7 * 86400
VERSION = 'qwen3-tts-06b-8bit-serena-v2'
PREFIX = 'ANTOU_TTS '


def readable_text(text: str) -> str:
    """保留正文语义；不读标记、长代码和网址，不调用模型改写。"""
    text = re.sub(r'```[\s\S]*?(?:```|$)', '。代码段请查看原文。', text)
    text = re.sub(r'!\[([^\]]*)\]\([^\n)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^\n)]*\)', r'\1', text)
    text = re.sub(r'https?://[^\s<>]+', '', text)
    text = re.sub(r'(?<!\w)(?:~/|/(?:Users|private|tmp|var|home)/)[^\s，。；、）)]+', '本地文件', text)
    text = re.sub(r'(?m)^\s*\|?[\s:|\-]+\|\s*$', '', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)', '', text)
    text = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', '', text)
    text = text.replace('|', '，').replace('**', '').replace('__', '').replace('`', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n\s*\n+', '\n', text).strip()


def chunks(text: str, size: int = 80) -> list[str]:
    """第一句先生成；后续按标点合并，控制单次推理长度。"""
    sentences = [s.strip() for s in re.findall(r'[^。！？!?；;\n]+[。！？!?；;\n]*', text) if s.strip()]
    out, buf = [], ''
    for sentence in sentences:
        for start in range(0, len(sentence), size):
            part = sentence[start:start + size]
            if not out and not buf:
                out.append(part)
            elif len(buf) + len(part) > size:
                out.append(buf)
                buf = part
            else:
                buf += part
    if buf:
        out.append(buf)
    return out


class Speech:
    def __init__(self, root: Path, *, python: Path | None = None, max_bytes: int = MAX_BYTES, keep: int = KEEP):
        self.root = root
        self.python = python or Path(__file__).resolve().parents[2] / '.venv-tts/bin/python'
        self.max_bytes, self.keep = max_bytes, keep
        self.jobs: dict[str, asyncio.Task] = {}
        self.readers: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.requests_lock = asyncio.Lock()
        self.listeners: dict[str, tuple[str, float]] = {}
        self.released: dict[str, float] = {}
        self.proc = None
        self.last_work = 0.0

    def directory(self, key: str) -> Path:
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('找不到这段音频')
        return self.root / key

    def meta(self, key: str) -> dict:
        try:
            return json.loads((self.directory(key) / 'meta.json').read_text())
        except (OSError, json.JSONDecodeError):
            raise ValueError('音频缓存已清理，请重新点朗读') from None

    def _save(self, key: str, meta: dict):
        d = self.directory(key)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = d / 'meta.tmp'
        tmp.write_text(json.dumps(meta, ensure_ascii=False))
        tmp.replace(d / 'meta.json')

    def touch(self, key: str, *, playing: bool = False) -> dict:
        m = self.meta(key)
        m['accessed'] = time.time()
        # 锁屏后 JS 可能暂停：为整个剩余播放时长留保护期。
        if playing:
            m['protected_until'] = time.time() + max(900, min(86400, m.get('duration', 0) * 2 + 300))
        # 暂停或下载不缩短保护期：另一台设备可能还在听同一段音频。
        self._save(key, m)
        return m

    def cleanup(self, reserve: int = 0) -> bool:
        now, entries, total = time.time(), [], 0
        if not self.root.exists():
            return reserve <= self.max_bytes
        for d in self.root.iterdir():
            if not d.is_dir() or d.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}', d.name):
                continue
            try:
                m = self.meta(d.name)
            except ValueError:
                m = {'accessed': d.stat().st_mtime}
            protected = d.name in self.jobs or self.readers.get(d.name, 0) or m.get('protected_until', 0) > now
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            if not protected and now - m.get('accessed', 0) > self.keep:
                shutil.rmtree(d)
                continue
            entries.append((m.get('accessed', 0), d, size, protected))
            total += size
        for _, d, size, protected in sorted(entries):
            if total + reserve <= self.max_bytes:
                break
            if not protected:
                shutil.rmtree(d)
                total -= size
        return total + reserve <= self.max_bytes

    def status(self, key: str) -> dict:
        m = self.meta(key)
        if m['state'] in ('queued', 'generating') and key not in self.jobs:
            m.update(state='error', error='上次生成已中断，请重新点朗读', protected_until=0)
            self._save(key, m)
        result = {k: m.get(k) for k in ('key', 'state', 'duration', 'parts', 'error', 'first_seconds', 'generation_seconds')}
        ahead = 0
        if m['state'] == 'queued':
            for other, task in self.jobs.items():
                if other == key:
                    break
                if not task.done():
                    ahead += 1
        result['queue_ahead'] = ahead
        result['wait_seconds'] = max(0, int(time.time() - m.get('created', time.time())))
        return result

    def renew(self, key: str, listener: str):
        if listener in self.listeners and self.listeners[listener][0] == key:
            self.listeners[listener] = (key, time.time())

    async def _drop_listener(self, listener: str):
        lease = self.listeners.pop(listener, None)
        if not lease:
            return
        key = lease[0]
        if any(k == key for k, _ in self.listeners.values()):
            return
        task = self.jobs.get(key)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.jobs.get(key) is task:
                self.jobs.pop(key, None)
            try:
                m = self.meta(key)
            except ValueError:  # 同时删除对话时，缓存可能已被清理。
                return
            if m.get('state') in ('queued', 'generating'):
                m.update(state='error', error='已取消朗读，可重新生成', protected_until=0)
                self._save(key, m)

    async def release(self, listener: str):
        async with self.requests_lock:
            # 关闭可能比开始请求先抵达；短期记住作废的请求，禁止它迟到后复活。
            self.released[listener] = time.time() + 300
            await self._drop_listener(listener)

    async def expire_listeners(self):
        async with self.requests_lock:
            now = time.time()
            self.released = {c: until for c, until in self.released.items() if until > now}
            for client, (key, seen) in list(self.listeners.items()):
                if now - seen > 120 and not self.readers.get(key):
                    await self._drop_listener(client)

    async def start(self, num: int, text: str, *, listener: str = '') -> dict:
        async with self.requests_lock:
            if listener and self.released.get(listener, 0) > time.time():
                raise ValueError('这次朗读已关闭')
            result = await self._start(num, text)
            if listener:
                if listener in self.listeners and self.listeners[listener][0] != result['key']:
                    await self._drop_listener(listener)
                self.listeners[listener] = (result['key'], time.time())
            return result

    async def _start(self, num: int, text: str) -> dict:
        text = readable_text(text)
        if not text:
            raise ValueError('这条回复没有可朗读的正文')
        if len(text) > 30000:
            raise ValueError('这条回复超过 3 万字，请分段收听')
        key = hashlib.sha256(f'{VERSION}:{num}:{text}'.encode()).hexdigest()
        try:
            m = self.meta(key)
            if key in self.jobs or m.get('state') == 'ready' and (self.directory(key) / 'audio.mp3').is_file():
                self.touch(key, playing=True)
                return self.status(key)
        except ValueError:
            pass
        if not self.python.is_file():
            raise ValueError('本机语音环境未安装，请在 Mac 运行 scripts/setup_speech.sh')
        if len(self.jobs) >= 3:
            raise ValueError('已有三段语音在生成，请稍后再试')
        # 为所有排队中的任务预留空间，不以删除正在播放的音频来腾空间。
        if not self.cleanup(reserve=(MAX_AUDIO + 4 * 1024 ** 2) * (len(self.jobs) + 1)):
            raise ValueError('音频缓存正在使用，暂时无法腾出足够空间，请稍后再试')
        d = self.directory(key)
        if d.exists():
            shutil.rmtree(d)
        self._save(key, dict(key=key, num=num, state='queued', created=time.time(), accessed=time.time(), protected_until=0,
                             duration=0, parts=0, error='', first_seconds=None, generation_seconds=None))
        task = asyncio.create_task(self._generate(key, chunks(text)))
        self.jobs[key] = task
        task.add_done_callback(lambda t: self.jobs.pop(key, None) if self.jobs.get(key) is t else None)
        return self.status(key)

    async def _worker(self):
        if not self.proc or self.proc.returncode is not None:
            self.proc = await asyncio.create_subprocess_exec(
                str(self.python), str(Path(__file__).with_name('speech_worker.py')),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        return self.proc

    async def _event(self, proc) -> dict:
        while line := await proc.stdout.readline():
            if line.startswith(PREFIX.encode()):
                return json.loads(line[len(PREFIX):])
        raise RuntimeError('语音进程已退出，请重新点朗读')

    async def _kill_worker(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            p.kill()
            await p.wait()

    async def _generate(self, key: str, parts: list[str]):
        began = time.monotonic()
        try:
            async with self.lock:
                m = self.meta(key)
                m['state'] = 'generating'
                self._save(key, m)
                proc = await self._worker()
                proc.stdin.write((json.dumps({'parts': parts, 'out': str(self.directory(key))}, ensure_ascii=False) + '\n').encode())
                await proc.stdin.drain()
                async with asyncio.timeout(1800):
                    while True:
                        event = await self._event(proc)
                        if event.get('error'):
                            raise RuntimeError('本机语音生成失败，请重试；重复失败请检查语音环境')
                        if event.get('done'):
                            break
                        m = self.meta(key)  # 保留播放端刚刷新的访问时间
                        m.update(duration=event['duration'], parts=event['parts'])
                        if m['first_seconds'] is None:
                            m['first_seconds'] = round(time.monotonic() - began, 3)
                        self._save(key, m)
                audio = self.directory(key) / 'audio.part'
                if not audio.is_file() or not audio.stat().st_size:
                    raise RuntimeError('没有生成音频，请重试')
                audio.replace(self.directory(key) / 'audio.mp3')
                m = self.meta(key)
                m.update(state='ready', generation_seconds=round(time.monotonic() - began, 3))
                if m.get('protected_until', 0) > time.time():
                    m['protected_until'] = time.time() + max(900, min(86400, m['duration'] * 2 + 300))
                self._save(key, m)
                self.last_work = time.time()
        except (Exception, asyncio.CancelledError) as exc:
            # 生成中断后丢弃模型进程，避免下一任务读到上次输出。
            # 排队任务取消时不能杀死另一个正在工作的进程。
            try:
                m = self.meta(key)
                if m.get('state') == 'generating':
                    await self._kill_worker()
                m.update(state='error', error=str(exc) if isinstance(exc, RuntimeError) else '语音生成中断，请重试', protected_until=0)
                self._save(key, m)
                for name in ('audio.part', 'segment.mp3'):
                    (self.directory(key) / name).unlink(missing_ok=True)
            except ValueError:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.last_work = time.time()

    async def remove_session(self, num: int):
        if not self.root.exists():
            return
        for d in list(self.root.iterdir()):
            try:
                if self.meta(d.name).get('num') != num:
                    continue
            except ValueError:
                continue
            if task := self.jobs.get(d.name):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.jobs.pop(d.name, None)
            shutil.rmtree(d, ignore_errors=True)

    async def maintenance(self):
        last_clean = 0.0
        while True:
            await self.expire_listeners()
            if time.time() - last_clean >= 60:
                self.cleanup()
                last_clean = time.time()
            if not self.jobs and self.proc and time.time() - self.last_work > 600:
                await self._kill_worker()  # 不听时释放模型内存
            await asyncio.sleep(5)

    async def close(self):
        for task in list(self.jobs.values()):
            task.cancel()
        await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)
        await self._kill_worker()
