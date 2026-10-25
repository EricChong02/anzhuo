"""对话登记与运行：每个对话一个编号，背后是一个 Claude Code 对话；几个对话可同时干活，超过上限的排队。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, SystemMessage, TextBlock,
    ToolUseBlock, create_sdk_mcp_server, tool,
)

from .folders import display
from .report import Turn, brief

log = logging.getLogger(__name__)

STALE = 7 * 86400  # 7 天没动的对话自动收起：不进列表，小E 分派时也不考虑

APPEND = """回复一律用简体中文；专有名词、代码、命令、路径保持原样。
这一轮如果只是聊天、打招呼或直接回答问题，没有动手做事，就直接自然地回答，不写下面的五段。
这一轮如果动手做了事（改文件、跑命令、查资料、做截图等），最后一段回复按下面五段写，用简体中文，写给不看代码的读者：
要你做的：需要用户做或决定的事；没有就写“没有”。
做了什么：按顺序列出这一轮做的事。
改了哪些：用读者看得懂的话说改了哪里，不列文件路径。
自查结果：跑了哪些检查、结果如何；哪些没有验证要直说。
截图：做了截图、或有能说明结果的图片时，调用 send_images 工具把图片的绝对路径交出去（最多 6 张），这里只写每张图是什么。
某一段没有内容（比如没有要用户做的、没有改文件、没有截图），这一段整段不写，不要写“没有”“无”。
需要用户在几个方案里选一个时，先把各方案做成截图并用 send_images 交出，回复第一行写“要你定：”加一句问题，然后列出选项和你的推荐，停下等回复。"""

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@dataclass
class Session:
    num: int
    name: str
    cwd: str
    add_dirs: list[str] = field(default_factory=list)
    claude_id: str | None = None
    status: str = "idle"  # idle / running / queued / error
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    last_title: str = ""
    model: str | None = None  # None = 用 ~/.claude/settings.json 里的默认
    effort: str | None = None
    context_pct: float | None = None
    hidden: bool = False  # 你发“删 N”收起的；聊天记录还在，发“#N …”就回来

    @property
    def label(self) -> str:
        return f"#{self.num} {self.name}"


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[int, Session] = {}
        self.current: int | None = None
        self.next_num = 1
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        self.sessions = {int(k): Session(**v) for k, v in raw.get("sessions", {}).items()}
        self.current = raw.get("current")
        self.next_num = raw.get("next_num", max(self.sessions, default=0) + 1)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "sessions": {str(k): asdict(v) for k, v in self.sessions.items()},
            "current": self.current, "next_num": self.next_num,
        }, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    def reserve_num(self) -> int:
        n = self.next_num
        self.next_num += 1
        return n

    def add(self, s: Session) -> Session:
        self.sessions[s.num] = s
        self.current = s.num
        self.save()
        return s

    def get(self, num: int | None) -> Session | None:
        return self.sessions.get(num) if num is not None else None

    def visible(self) -> list[Session]:
        """没被收起的对话：没删、7 天内动过（正在做或排队的总算在内）。"""
        now = time.time()
        return [s for s in self.sessions.values()
                if not s.hidden and (now - s.updated < STALE or s.status in ("running", "queued"))]


@dataclass
class Job:
    text: str
    images: list[Path] = field(default_factory=list)
    finished: bool = False


Notify = Callable[[Session, Turn], Awaitable[None]]


class Runner:
    def __init__(self, registry: Registry, on_done: Notify, max_parallel: int = 3, default_effort: str | None = None):
        self.registry = registry
        self.on_done = on_done
        self.sem = asyncio.Semaphore(max_parallel)
        self.queues: dict[int, asyncio.Queue[Job]] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.clients: dict[int, ClaudeSDKClient] = {}
        self.turns: dict[int, Turn] = {}
        self.limits: dict[str, tuple[float, str | None]] = {}  # 额度：类型 → (已用比例, 何时重置)
        self._usage_at = 0.0
        self.default_effort = default_effort or user_setting("effortLevel") or ""

    def busy(self, num: int) -> bool:
        return num in self.turns or not self.queues.get(num, asyncio.Queue()).empty()

    def submit(self, s: Session, job: Job) -> int:
        """放进这个对话的队列，返回前面还排着几件。"""
        q = self.queues.setdefault(s.num, asyncio.Queue())
        ahead = q.qsize() + (1 if s.num in self.turns else 0)
        q.put_nowait(job)
        if s.num not in self.workers or self.workers[s.num].done():
            self.workers[s.num] = asyncio.create_task(self._worker(s.num))
        return ahead

    async def stop(self, num: int) -> bool:
        q = self.queues.get(num)
        while q and not q.empty():
            q.get_nowait()
        client = self.clients.get(num)
        if num in self.turns:
            self.turns[num].interrupted = True
        if client:
            await client.interrupt()
            return True
        return False

    async def _worker(self, num: int) -> None:
        q = self.queues[num]
        while not q.empty():
            job = await q.get()
            s = self.registry.get(num)
            if not s:
                continue
            s.status = "queued"
            self.registry.save()
            async with self.sem:
                turn = Turn(started=time.time(), request=_request_title(job))
                self.turns[num] = turn
                s.status = "running"
                self.registry.save()
                try:
                    await self._run(s, job, turn)
                except Exception as e:  # noqa: BLE001 - 任何失败都要报给用户
                    log.exception("对话 #%s 运行失败", num)
                    turn.is_error = True
                    turn.error = f"{type(e).__name__}: {str(e)[:300]}"
                finally:
                    turn.ended = time.time()
                    self.turns.pop(num, None)
                    self.clients.pop(num, None)
                    s.status = "error" if turn.is_error else "idle"
                    s.updated = time.time()
                    self.registry.save()
            await self.refresh_usage()
            job.finished = True
            await self.on_done(s, turn)

    async def refresh_usage(self, force: bool = False) -> None:
        """用本机 Claude Code 的 /usage 查订阅额度（不调模型、不耗额度）；2 分钟内不重复查。"""
        if not force and time.time() - self._usage_at < 120:
            return
        self._usage_at = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                str(_cli_path()), "-p", "/usage", cwd=str(Path.home()),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
            self.limits.update(parse_usage(out.decode(errors="replace")))
        except Exception:  # noqa: BLE001 - 查不到额度不影响报告
            log.debug("查额度失败", exc_info=True)

    def _image_tool(self, turn: Turn):
        @tool("send_images", "把截图或图片交给用户看。paths 是本机图片的绝对路径（png/jpg），最多 6 张，按想让用户看的顺序排。",
              {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
               "required": ["paths"]})
        async def send_images(args):
            ok, bad = [], []
            for p in args.get("paths", []):
                path = Path(p).expanduser()
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and len(turn.images) + len(ok) < 6:
                    ok.append(str(path))
                else:
                    bad.append(p)
            turn.images.extend(ok)
            msg = f"已收下 {len(ok)} 张。"
            if bad:
                msg += f" 没收下：{', '.join(bad)}（不存在、不是图片或超过 6 张）"
            return {"content": [{"type": "text", "text": msg}]}

        return create_sdk_mcp_server("xiaoe", tools=[send_images])

    async def _prompt(self, job: Job):
        content: list[dict] = []
        for p in job.images:
            mt = mimetypes.guess_type(p.name)[0] or "image/png"
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                         "data": base64.b64encode(p.read_bytes()).decode()}})
        content.append({"type": "text", "text": job.text or "（见图）"})
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    async def _run(self, s: Session, job: Job, turn: Turn) -> None:
        options = ClaudeAgentOptions(
            cwd=s.cwd,
            add_dirs=s.add_dirs,
            model=s.model,
            effort=s.effort or self.default_effort or None,
            resume=s.claude_id,
            setting_sources=["user", "project", "local"],
            permission_mode="bypassPermissions",
            system_prompt={"type": "preset", "preset": "claude_code", "append": APPEND},
            mcp_servers={"xiaoe": self._image_tool(turn)},
            stderr=lambda line: log.debug("claude: %s", line),
        )
        last_text = ""
        async with ClaudeSDKClient(options=options) as client:
            self.clients[s.num] = client
            await client.query(self._prompt(job))
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage):
                    if "compact" in msg.subtype:
                        turn.compacted = True
                    elif msg.subtype == "init" and not turn.model:
                        turn.model = str(msg.data.get("model") or "")
                elif isinstance(msg, AssistantMessage):
                    if msg.model:
                        turn.model = msg.model
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock) and b.text.strip()]
                    if texts:
                        last_text = "\n".join(texts)
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            if b.name in EDIT_TOOLS:
                                fp = b.input.get("file_path") or b.input.get("notebook_path")
                                if fp:
                                    turn.changed_files.append(fp)
                            elif b.name == "Bash":
                                turn.commands.append(str(b.input.get("command", ""))[:200])
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        s.claude_id = msg.session_id
                    if msg.is_error and not turn.interrupted:
                        turn.is_error = True
                        turn.error = (msg.result or "; ".join(msg.errors or []) or msg.subtype or "未知错误")[:300]
                    elif msg.result:
                        last_text = msg.result
            try:
                usage = await client.get_context_usage()
                turn.context_pct = s.context_pct = float(usage["percentage"])
            except Exception:  # noqa: BLE001 - 拿不到上下文用量不影响报告
                log.debug("取上下文用量失败", exc_info=True)
        turn.final_text = last_text
        turn.effort = s.effort or self.default_effort


_MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _cli_path() -> Path:
    import claude_agent_sdk
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


def _reset_cn(text: str) -> str | None:
    m = re.search(r"resets (\w{3}) (\d+)(?: at (\d+)(?::(\d+))?(am|pm))?", text)
    if not m:
        return None
    mon, day, hh, mm, ap = m.groups()
    out = f"{_MONTHS.get(mon, 0)}月{day}日"
    if hh:
        h = int(hh) % 12 + (12 if ap == "pm" else 0)
        out += f" {h:02d}:{int(mm or 0):02d}"
    return out


def parse_usage(text: str) -> dict[str, tuple[float, str | None]]:
    """解析 /usage 输出里的“Current session”（5 小时）和“Current week (all models)”（本周）。"""
    got = {}
    for line in text.splitlines():
        m = re.search(r"(\d+(?:\.\d+)?)% used", line)
        if not m:
            continue
        if line.strip().startswith("Current session"):
            got["five_hour"] = (float(m.group(1)) / 100, _reset_cn(line))
        elif line.strip().startswith("Current week (all models)"):
            got["seven_day"] = (float(m.group(1)) / 100, _reset_cn(line))
    return got


def user_setting(key: str) -> str | None:
    """读 ~/.claude/settings.json 里的默认值（模型、深度），只用来显示。"""
    try:
        return json.loads((Path.home() / ".claude/settings.json").read_text()).get(key)
    except (OSError, ValueError):
        return None


def _request_title(job: Job) -> str:
    return brief(job.text) or ("看图" if job.images else "")


def describe(s: Session, running: bool) -> str:
    state = {"idle": "空闲", "running": "在做", "queued": "排队", "error": "上次出错"}.get(s.status, s.status)
    if running:
        state = "在做"
    extra = f" ＋{len(s.add_dirs)} 个文件夹" if s.add_dirs else ""
    ctx = f"　上下文 {s.context_pct:.0f}%" if s.context_pct is not None else ""
    return f"#{s.num} {s.name}　{state}　{display(Path(s.cwd))}{extra}{ctx}"
