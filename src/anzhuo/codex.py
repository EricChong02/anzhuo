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


def codex_bin() -> Path | None:
    """找 Codex：macOS 用 ChatGPT 桌面版自带的；别的系统用 PATH 里的 codex（npm 装的 @openai/codex）。
    Windows 上 npm 只放一个 codex.cmd 外壳，直接起它不行，要找它包里真正的 codex.exe。"""
    if CODEX.exists():
        return CODEX
    import shutil
    if os.name == "nt":
        for shim in filter(None, (shutil.which("codex.cmd"), shutil.which("codex"))):
            root = Path(shim).parent / "node_modules" / "@openai" / "codex"
            if exe := next(iter(sorted(root.glob("**/vendor/*/bin/codex.exe"))), None):
                return exe
            if exe := next(iter(sorted(root.glob("**/codex.exe"))), None):
                return exe
        found = shutil.which("codex.exe")
        return Path(found) if found else None
    found = shutil.which("codex")
    return Path(found) if found else None


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


MODELS_SEEN: dict[str, str] = {}   # Codex 能用的型号 → 显示名（启动时问一次 model/list；新的排前面）


async def probe_models() -> None:
    """问 Codex 现在能用哪些型号（Sol / Terra / Luna / Astra 各代），网页菜单、总管选型号用。不花额度。"""
    c = CodexConn(str(Path.home()), lambda m, p: None)
    try:
        await c.start()
        r = await c.request("model/list", {}, timeout=30)
        items = r.get("data") if isinstance(r, dict) else r
        MODELS_SEEN.clear()
        MODELS_SEEN.update({str(x["id"]): str(x.get("displayName") or x["id"]) for x in items or []
                            if isinstance(x, dict) and x.get("id") and not x.get("hidden")})
    except Exception:  # noqa: BLE001 - 问不到就只用 Codex 自己配置的
        log.debug("读 Codex 型号失败", exc_info=True)
    finally:
        await c.close()


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
            t = time.localtime(at) if at else None   # 不用 %-m：Windows 的 strftime 不认
            out[name] = (float(w["usedPercent"]) / 100, f"{t.tm_mon}月{t.tm_mday}日 {t.tm_hour:02d}:{t.tm_min:02d}" if t else None)
    return out


class CodexConn:
    def __init__(self, cwd: str, on_event: Callable[[str, dict], Awaitable[None] | None],
                 on_request: Callable[[str, dict], Awaitable[dict]] | None = None, tools: list[dict] | None = None):
        self.cwd = cwd
        self.tools = tools or []   # 交给 Codex 的自定义工具（dynamicTools，实验接口）；调用时走 item/tool/call 反过来问
        self.on_event = on_event
        self.on_request = on_request  # Codex 反过来问（跑命令要不要批准之类）：返回要回给它的结果
        self.model = ""
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._wait: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()   # 读输出、读错误输出、回答它反过来问的：留着引用，关连接时一起收掉
        self._stderr = bytearray()
        self.thread_id = ""
        self.turn_id = ""
        self.done = asyncio.Event()   # 这一轮结束（完成 / 被停 / 失败）
        self.turn_status = ""
        self.turn_error = ""

    async def start(self) -> None:
        exe = codex_bin()
        if not exe:
            raise CodexError("没找到 Codex：macOS 装 ChatGPT 桌面版，其他系统用 npm i -g @openai/codex 装好并登录")
        self.proc = await asyncio.create_subprocess_exec(
            str(exe), "app-server", cwd=self.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
        self._reader = self._spawn(self._read())
        self._spawn(self._drain())
        init = {"clientInfo": {"name": "anzhuo", "version": "1"}}
        if self.tools:   # 自定义工具是实验接口，要先声明
            init["capabilities"] = {"experimentalApi": True}
        await self.request("initialize", init)
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
                    self._spawn(self._answer(msg["id"], method, params))
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
            await self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "远窗不处理这个请求"}})
        else:
            await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def stderr_tail(self) -> str:
        lines = bytes(self._stderr).decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else "没有更多信息"

    async def open_thread(self, thread_id: str | None, instructions: str, model: str | None = None) -> str:
        # untrusted：除了只读的命令，跑之前都来问一声 —— 远窗自动放行，只把危险的（删、推、改库……）交给你点头
        common = {"cwd": self.cwd, "sandbox": "workspace-write", "approvalPolicy": "untrusted",
                  "config": {"sandbox_workspace_write": {"network_access": True}}, **({"model": model} if model else {})}
        if thread_id:
            r = await self.request("thread/resume", {"threadId": thread_id, **common})
        else:
            r = await self.request("thread/start", {**common, "developerInstructions": instructions,
                                                    **({"dynamicTools": self.tools} if self.tools else {})})
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

    async def start_turn(self, text: str, images: list[Path], effort: str | None, model: str | None = None) -> str:
        params = {"threadId": self.thread_id, "input": self._input(text, images)}
        if effort:
            params["effort"] = effort
        if model:   # 指定了型号（Sol / Terra / Luna …）：这一轮起用它；不指定用 Codex 自己配置的
            params["model"] = model
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

    def _spawn(self, coro) -> asyncio.Task:
        """开一个后台任务并留住引用（只靠事件循环的弱引用，任务可能半路被回收）；做完自己从集合里拿掉。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        # 进程没了，读输出的、还在等你点头的回答都收掉，免得挂在事件循环里
        me = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not me and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
