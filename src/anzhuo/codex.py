"""Codex 的常驻连接（app-server，stdio 上一行一条 JSON-RPC）：开 / 接着一个会话、开一轮、
干活中途插话（turn/steer）、停下（turn/interrupt），边做边把事件交给回调。用的是 ChatGPT 桌面版自带的 Codex。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tomllib
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")


def configured_model() -> str:
    """新会话未指定模型时，Codex 使用本机配置；只读取模型字段。"""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    try:
        model = tomllib.loads((home / "config.toml").read_text()).get("model")
        return model if isinstance(model, str) else ""
    except (OSError, ValueError):
        return ""


class CodexError(RuntimeError):
    pass


async def rate_limits() -> dict:
    """ChatGPT 会员的 Codex 额度：{"five_hour": (已用比例, "几月几日 几点"), "seven_day": (...)}；读不到返回空。"""
    import time
    c = CodexConn(str(Path.home()), lambda m, p: None)
    try:
        await c.start()
        r = (await c.request("account/rateLimits/read", {}, timeout=30)).get("rateLimits") or {}
    except Exception:  # noqa: BLE001 - 读不到额度不影响干活
        log.debug("读 Codex 额度失败", exc_info=True)
        return {}
    finally:
        await c.close()
    return limits_from(r)


def limits_from(r: dict) -> dict:
    import time
    out = {}
    for key, name in (("primary", "five_hour"), ("secondary", "seven_day")):
        w = r.get(key) or {}
        if w.get("usedPercent") is not None:
            at = w.get("resetsAt")
            out[name] = (float(w["usedPercent"]) / 100,
                         time.strftime("%-m月%-d日 %H:%M", time.localtime(at)) if at else None)
    return out


class CodexConn:
    def __init__(self, cwd: str, on_event: Callable[[str, dict], Awaitable[None] | None],
                 on_request: Callable[[str, dict], Awaitable[dict]] | None = None):
        self.cwd = cwd
        self.on_event = on_event
        self.on_request = on_request  # Codex 反过来问（跑命令要不要批准之类）：返回要回给它的结果
        self.model = ""
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._wait: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._stderr = bytearray()
        self.thread_id = ""
        self.turn_id = ""
        self.done = asyncio.Event()   # 这一轮结束（完成 / 被停 / 失败）
        self.turn_status = ""
        self.turn_error = ""

    async def start(self) -> None:
        if not CODEX.exists():
            raise CodexError("没找到 ChatGPT 桌面版自带的 Codex（/Applications/ChatGPT.app）")
        self.proc = await asyncio.create_subprocess_exec(
            str(CODEX), "app-server", cwd=self.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read())
        asyncio.create_task(self._drain())
        await self.request("initialize", {"clientInfo": {"name": "anzhuo", "version": "1"}})
        await self._send({"jsonrpc": "2.0", "method": "initialized"})

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 120) -> dict:
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._wait[self._id] = fut
        await self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise CodexError(f"{method}：{(msg['error'] or {}).get('message') or msg['error']}")
        return msg.get("result") or {}

    async def _drain(self) -> None:
        while self.proc and self.proc.stderr and (chunk := await self.proc.stderr.read(65536)):
            self._stderr = (self._stderr + chunk)[-20000:]

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for line in self.proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg) and msg["id"] in self._wait:
                    self._wait.pop(msg["id"]).set_result(msg)
                    continue
                method, params = msg.get("method", ""), msg.get("params") or {}
                if "id" in msg and method:   # Codex 反过来问：另开一路处理（可能要等你点头），不挡住后面的消息
                    asyncio.create_task(self._answer(msg["id"], method, params))
                    continue
                if method == "turn/completed":
                    turn = params.get("turn") or {}
                    self.turn_status = turn.get("status") or ""
                    self.turn_error = str((turn.get("error") or {}).get("message") or "") if isinstance(turn.get("error"), dict) else str(turn.get("error") or "")
                if method:
                    try:
                        r = self.on_event(method, params)
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:  # noqa: BLE001 - 一条事件出错不影响整轮
                        log.exception("处理 Codex 事件失败：%s", method)
                if method in ("turn/completed", "thread/compacted"):
                    self.done.set()
        finally:
            self.done.set()
            for f in self._wait.values():
                if not f.done():
                    f.set_result({"error": {"message": "Codex 退出了：" + self.stderr_tail()}})

    async def _answer(self, rid, method: str, params: dict) -> None:
        try:
            result = await self.on_request(method, params) if self.on_request else None
        except Exception:  # noqa: BLE001
            log.exception("回答 Codex 的请求失败：%s", method)
            result = None
        if result is None:
            await self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "案头不处理这个请求"}})
        else:
            await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def stderr_tail(self) -> str:
        lines = bytes(self._stderr).decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else "没有更多信息"

    async def open_thread(self, thread_id: str | None, instructions: str) -> str:
        # untrusted：除了只读的命令，跑之前都来问一声 —— 案头自动放行，只把危险的（删、推、改库……）交给你点头
        common = {"cwd": self.cwd, "sandbox": "workspace-write", "approvalPolicy": "untrusted",
                  "config": {"sandbox_workspace_write": {"network_access": True}}}
        if thread_id:
            r = await self.request("thread/resume", {"threadId": thread_id, **common})
        else:
            r = await self.request("thread/start", {**common, "developerInstructions": instructions})
        self.thread_id = (r.get("thread") or {}).get("id") or thread_id or ""
        self.model = str(r.get("model") or "")
        return self.thread_id

    async def fork_thread(self, thread_id: str, last_turn_id: str) -> str:
        r = await self.request("thread/fork", {"threadId": thread_id, "lastTurnId": last_turn_id,
            "cwd": self.cwd, "sandbox": "workspace-write", "approvalPolicy": "untrusted",
            "config": {"sandbox_workspace_write": {"network_access": True}}})
        tid = (r.get("thread") or {}).get("id")
        if not tid or tid == thread_id:
            raise CodexError("分叉没有返回新的对话编号")
        self.thread_id = tid
        self.model = str(r.get("model") or "")
        return tid

    async def compact(self) -> None:
        """压缩这个会话的上下文；压完会发 thread/compacted。"""
        await self.request("thread/compact/start", {"threadId": self.thread_id})

    @staticmethod
    def _input(text: str, images: list[Path]) -> list[dict]:
        return [{"type": "text", "text": text or "（见图）"}] + [{"type": "localImage", "path": str(p)} for p in images]

    async def start_turn(self, text: str, images: list[Path], effort: str | None) -> str:
        params = {"threadId": self.thread_id, "input": self._input(text, images)}
        if effort:
            params["effort"] = effort
        r = await self.request("turn/start", params)
        self.turn_id = (r.get("turn") or {}).get("id") or ""
        return self.turn_id

    async def steer(self, text: str, images: list[Path]) -> None:
        """干活中途插话：交给正在进行的这一轮，它会在后面的步骤里考虑进去。"""
        await self.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                          "input": self._input(text, images)})

    async def interrupt(self) -> None:
        if self.thread_id and self.turn_id and not self.done.is_set():
            await self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=20)

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
