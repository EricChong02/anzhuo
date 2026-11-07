"""ACP 引擎：用 Agent Client Protocol（stdio 上一行一条 JSON-RPC）让别家的编程助手干活。

Claude（claude-agent-sdk）和 GPT（Codex app-server）照旧走各自的连接；新加的助手都走这里。
先接 Kimi（kimi acp）；要加别的，在 ENGINES 里登记启动命令即可。
ACP 没有“干活中途插话”，中途说的话排到下一轮；也没有回退 / 分叉。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import shutil
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# 引擎 → 名字、启动命令（第一个词在 PATH 里找）
ENGINES: dict[str, dict] = {
    "kimi": {"name": "Kimi", "cmd": ["kimi", "acp"]},
}


def is_acp(engine: str) -> bool:
    return engine in ENGINES


def command(engine: str) -> list[str] | None:
    spec = ENGINES.get(engine)
    if not spec:
        return None
    exe = shutil.which(spec["cmd"][0])
    return [exe, *spec["cmd"][1:]] if exe else None


def available(engine: str) -> bool:
    return command(engine) is not None


class AcpError(RuntimeError):
    pass


Update = Callable[[dict], Awaitable[None] | None]
Permission = Callable[[dict], Awaitable[str | None]]   # 返回选中的 optionId；None = 拒绝 / 取消


class AcpConn:
    def __init__(self, engine: str, cwd: str, on_update: Update, on_permission: Permission | None = None,
                 add_dirs: list[str] | None = None):
        self.engine, self.cwd, self.add_dirs = engine, cwd, add_dirs or []
        self.on_update, self.on_permission = on_update, on_permission
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._wait: dict[int, asyncio.Future] = {}
        self._stderr = bytearray()
        self.caps: dict = {}
        self.session_id = ""
        self.options: list[dict] = []   # 会话的配置项（模型、思考、模式……）
        self.quiet = False   # 载入旧会话时它会把历史重放一遍：这期间的事件不算这一轮的

    async def start(self) -> None:
        cmd = command(self.engine)
        if not cmd:
            raise AcpError(f"没找到 {ENGINES.get(self.engine, {}).get('name', self.engine)} 的命令行（{ENGINES.get(self.engine, {}).get('cmd', ['?'])[0]}）")
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
        asyncio.create_task(self._read())
        asyncio.create_task(self._drain())
        r = await self.request("initialize", {
            "protocolVersion": 1, "clientInfo": {"name": "anzhuo", "version": "1"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, timeout=60)
        self.caps = r.get("agentCapabilities") or {}

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float | None = 120) -> dict:
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._wait[self._id] = fut
        await self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        msg = await (asyncio.wait_for(fut, timeout) if timeout else fut)
        if "error" in msg:
            err = msg["error"] or {}
            raise AcpError(f"{method}：{err.get('message') or err}")
        return msg.get("result") or {}

    async def _drain(self) -> None:
        while self.proc and self.proc.stderr and (chunk := await self.proc.stderr.read(65536)):
            self._stderr = (self._stderr + chunk)[-20000:]

    def stderr_tail(self) -> str:
        lines = bytes(self._stderr).decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else "没有更多信息"

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
                if "id" in msg and method:   # 它反过来问（要不要批准）：另开一路，不挡住后面的消息
                    asyncio.create_task(self._answer(msg["id"], method, params))
                elif method == "session/update" and not self.quiet:
                    try:
                        r = self.on_update(params.get("update") or {})
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:  # noqa: BLE001 - 一条事件出错不影响整轮
                        log.exception("处理 ACP 事件失败")
        finally:
            for f in self._wait.values():
                if not f.done():
                    f.set_result({"error": {"message": "助手退出了：" + self.stderr_tail()}})

    async def _answer(self, rid, method: str, params: dict) -> None:
        if method == "session/request_permission":
            try:
                pick = await self.on_permission(params) if self.on_permission else None
            except Exception:  # noqa: BLE001 - 出错就当不批准
                log.exception("处理批准请求失败")
                pick = None
            outcome = {"outcome": "selected", "optionId": pick} if pick else {"outcome": "cancelled"}
            await self._send({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})
            return
        await self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "远窗不处理这个请求"}})

    async def open_session(self, session_id: str | None) -> str:
        """接着旧会话（能 resume 就 resume，不行就 load），没有就新开。返回会话编号。"""
        base = {"cwd": self.cwd, "mcpServers": []}
        if self.add_dirs and (self.caps.get("sessionCapabilities") or {}).get("additionalDirectories") is not None:
            base["additionalDirectories"] = self.add_dirs
        r: dict = {}
        if session_id and (self.caps.get("sessionCapabilities") or {}).get("resume") is not None:
            r = await self.request("session/resume", {"sessionId": session_id, **base})
            self.session_id = session_id
        elif session_id and self.caps.get("loadSession"):
            self.quiet = True
            try:
                r = await self.request("session/load", {"sessionId": session_id, **base}, timeout=300)
            finally:
                self.quiet = False
            self.session_id = session_id
        else:
            r = await self.request("session/new", base)
            self.session_id = r.get("sessionId") or ""
        self.options = r.get("configOptions") or self.options
        if not self.session_id:
            raise AcpError("没拿到会话编号")
        return self.session_id

    def option(self, oid: str) -> dict | None:
        return next((o for o in self.options if o.get("id") == oid or o.get("category") == oid), None)

    async def set_option(self, oid: str, value: str) -> bool:
        """改一个配置项（模型、思考……）。没有这个配置项、没有这个取值、或已经是它，就什么都不做。"""
        o = self.option(oid)
        if not o or o.get("currentValue") == value or value not in [x.get("value") for x in o.get("options") or []]:
            return False
        r = await self.request("session/set_config_option", {"sessionId": self.session_id, "configId": o["id"], "value": value})
        self.options = r.get("configOptions") or self.options
        return True

    @staticmethod
    def blocks(text: str, images: list[Path]) -> list[dict]:
        out: list[dict] = [{"type": "text", "text": text or "（见图）"}]
        for p in images:
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            out.append({"type": "image", "mimeType": mime, "data": base64.b64encode(Path(p).read_bytes()).decode()})
        return out

    async def prompt(self, text: str, images: list[Path]) -> str:
        """说一句话，等这一轮做完；返回结束原因（end_turn / cancelled / max_tokens / refusal …）。"""
        r = await self.request("session/prompt", {"sessionId": self.session_id, "prompt": self.blocks(text, images)}, timeout=None)
        return str(r.get("stopReason") or "")

    async def cancel(self) -> None:
        if self.session_id and self.proc and self.proc.returncode is None:
            await self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
