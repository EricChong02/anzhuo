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
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, SystemMessage, TextBlock,
    ThinkingBlock, ToolUseBlock, create_sdk_mcp_server, tool,
)

from . import guard
from .folders import blocked, display
from .cards import step_label
from .report import Turn, brief

log = logging.getLogger(__name__)

STALE = 7 * 86400  # 7 天没动的对话自动收起：不进列表，小E 分派时也不考虑

APPEND = """回复一律用简体中文，思考和过程中的说明也用简体中文；专有名词、代码、命令、路径保持原样。
这一轮如果只是聊天、打招呼或直接回答问题，没有动手做事，就直接自然地回答，不写下面的五段。
这一轮如果动手做了事（改文件、跑命令、查资料、做截图等），最后一段回复按下面五段写，用简体中文，写给不看代码的读者：
要你做的：需要用户做或决定的事；没有就写“没有”。
做了什么：按顺序列出这一轮做的事。
改了哪些：用读者看得懂的话说改了哪里，不列文件路径。
自查结果：跑了哪些检查、结果如何；哪些没有验证要直说。
截图：做了截图、或有能说明结果的图片时，调用 send_images 工具把图片的绝对路径交出去（最多 6 张），这里只写每张图是什么。
某一段没有内容（比如没有要用户做的、没有改文件、没有截图），这一段整段不写，不要写“没有”“无”。
需要用户在几个方案里选一个时，先把各方案做成截图并用 send_images 交出，回复第一行写“要你定：”加一句问题，然后列出选项和你的推荐，停下等回复。
选项每个单独一行，写成“A. 一句话说明”“B. …”（最多 4 个）；推荐单独一行，写成“推荐：A，理由”。
用户要文件（PDF、文档、表格、原件等）时，用 send_files 把文件原样交给用户，不要只发截图；截图只用于看效果。"""

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
    unsent: str = ""  # 上一句根本没能开始（比如文件夹打不开、启动超时），下一句带上它，免得 Claude 不知道要做什么
    seen: float = 0.0  # 你最后一次在网页里看这个对话的时间；之后又做完一轮就算“没看的回复”
    awaiting: str = ""  # 上一轮最后在等你回答（问了什么）；你回了话就清掉
    last_summary: str = ""  # 上一轮你说了什么、它答了什么（各一句），给小E 分派时看
    from_desktop: bool = False  # 接上的是桌面端开的对话（不是小E 新开的）

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


class FolderBlocked(Exception):
    """要用的文件夹打不开：多半是 macOS 隐私保护在等你点“允许”。"""


@dataclass
class Job:
    text: str
    images: list[Path] = field(default_factory=list)
    finished: bool = False


Notify = Callable[[Session, Turn], Awaitable[None]]
Started = Callable[[Session, Turn, Job], Awaitable[None]]
CONFIRM_WAIT = 20 * 60  # 等你确认危险操作最多多久，过了当你不同意


class Runner:
    def __init__(self, registry: Registry, on_done: Notify, max_parallel: int = 3, default_effort: str | None = None,
                 on_start: Started | None = None, on_confirm: Notify | None = None):
        self.registry = registry
        self.on_confirm = on_confirm  # 有危险操作等你点头时（发通知、按钮卡）
        self.on_done = on_done
        self.on_start = on_start  # 开始干活时（发进度卡用）
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
        """这个对话正在做：直接插话给它（返回 -1），它手上这一步做完就看到；否则排队，返回前面还排着几件。"""
        turn, client = self.turns.get(s.num), self.clients.get(s.num)
        if turn and client and turn.accepting and not turn.interrupted and job.text != COMPACT:
            turn.injected += 1
            turn.timeline.append({"t": time.time(), "kind": "you", "text": job.text[:3000]})
            asyncio.create_task(self._inject(s, client, job))
            return -1
        q = self.queues.setdefault(s.num, asyncio.Queue())
        ahead = q.qsize() + (1 if s.num in self.turns else 0)
        q.put_nowait(job)
        if s.num not in self.workers or self.workers[s.num].done():
            self.workers[s.num] = asyncio.create_task(self._worker(s.num))
        return ahead

    async def _inject(self, s: Session, client: ClaudeSDKClient, job: Job) -> None:
        try:
            await client.query(self._prompt(job))
            job.finished = True
        except Exception:  # noqa: BLE001 - 插不进去就排到下一轮
            log.warning("插话给 #%s 失败，排到下一轮", s.num, exc_info=True)
            q = self.queues.setdefault(s.num, asyncio.Queue())
            q.put_nowait(job)

    def answer_confirm(self, num: int, allow: bool) -> bool:
        """你在手机 / 钉钉 / 网页上点了允许或不允许。"""
        turn = self.turns.get(num)
        if not turn or not turn.confirm or turn.confirm["fut"].done():
            return False
        turn.confirm["fut"].set_result(allow)
        return True

    def _guard(self, s: Session, turn: Turn):
        async def hook(data, tool_use_id, context):
            cmd = str((data.get("tool_input") or {}).get("command") or "")
            what = guard.danger(cmd)
            if not what:
                return {}
            fut = asyncio.get_running_loop().create_future()
            turn.confirm = {"what": what, "cmd": cmd[:600], "fut": fut}
            turn.timeline.append({"t": round(time.time() - turn.started, 1), "kind": "do", "text": f"等你确认：{what}",
                                  "detail": cmd[:400]})
            if self.on_confirm:
                asyncio.create_task(self.on_confirm(s, turn))
            try:
                allow = await asyncio.wait_for(asyncio.shield(fut), CONFIRM_WAIT)
            except asyncio.TimeoutError:
                allow = False
            finally:
                turn.confirm = None
            out = {"hookEventName": "PreToolUse"}
            if allow:
                out.update(permissionDecision="allow", permissionDecisionReason="用户确认可以执行")
            else:
                out.update(permissionDecision="deny", permissionDecisionReason=(
                    "用户没有同意这一步（或 20 分钟没回）。不要执行它，也不要换个写法绕过去；"
                    "能不做就用别的办法完成，必须做就在回复里说明为什么要这样做，等用户明确同意。"))
            return {"hookSpecificOutput": out}
        return hook

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
                if self.on_start:
                    asyncio.create_task(self.on_start(s, turn, job))
                if s.unsent:
                    job.text = f"{s.unsent}\n\n{job.text}"
                    s.unsent = ""
                started_before = s.claude_id
                try:
                    await self._run(s, job, turn)
                except FolderBlocked as e:
                    turn.is_error = True
                    turn.error = (f"打不开 {e}。下载、桌面、文稿这几个文件夹受 macOS 隐私保护，小E 在后台头一次用时，"
                                  "Mac 会弹窗等人点“允许”，没人点就卡住。回到电脑前给小E 授权一次就好（做法见说明的“文件夹权限”）。")
                except Exception as e:  # noqa: BLE001 - 任何失败都要报给用户
                    log.exception("对话 #%s 运行失败", num)
                    turn.is_error = True
                    turn.error = f"{type(e).__name__}: {str(e)[:300]}"
                    if "initialize" in turn.error:
                        turn.error = "Claude 一分钟内没启动起来（常见原因：Mac 在等权限弹窗，或网络不通）。原始信息：" + turn.error
                finally:
                    turn.ended = time.time()
                    self.turns.pop(num, None)
                    self.clients.pop(num, None)
                    if turn.is_error and s.claude_id == started_before and not turn.final_text:
                        s.unsent = job.text  # Claude 一个字都没收到，下次一起带上
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

        @tool("send_files", "把文件原样交给用户（PDF、Word、表格、压缩包等任何格式，用户能直接下载）。paths 是本机文件的绝对路径，最多 10 个，每个不超过 100MB。"
              "用户要的是文件本身时用这个，不要只发截图。",
              {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
               "required": ["paths"]})
        async def send_files(args):
            ok, bad = [], []
            for p in args.get("paths", []):
                path = Path(p).expanduser()
                if path.is_file() and path.stat().st_size <= 100 * 1024 * 1024 and len(turn.files) + len(ok) < 10:
                    ok.append(str(path))
                else:
                    bad.append(p)
            turn.files.extend(ok)
            msg = f"已收下 {len(ok)} 个文件。"
            if bad:
                msg += f" 没收下：{', '.join(bad)}（不存在、超过 100MB 或超过 10 个）"
            return {"content": [{"type": "text", "text": msg}]}

        return create_sdk_mcp_server("xiaoe", tools=[send_images, send_files])

    async def _prompt(self, job: Job):
        content: list[dict] = []
        for p in job.images:
            mt = mimetypes.guess_type(p.name)[0] or "image/png"
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                         "data": base64.b64encode(p.read_bytes()).decode()}})
        content.append({"type": "text", "text": job.text or "（见图）"})
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    async def _run(self, s: Session, job: Job, turn: Turn) -> None:
        if stuck := await asyncio.to_thread(blocked, [Path(s.cwd), *map(Path, s.add_dirs)]):
            raise FolderBlocked("、".join(display(p) for p in stuck))
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
            max_buffer_size=64 * 1024 * 1024,  # 默认 1MB：Claude 看一张大截图就超，整轮报错（10-25 #6 实际遇到）
            thinking={"type": "adaptive", "display": "summarized"},  # 要思考摘要（默认不给字），网页“工作过程”里看它怎么想的
            # 删东西、推代码、改数据库这类先问你（见 guard.py）；等你点的时间算在钩子里，超时就当不同意
            hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[self._guard(s, turn)], timeout=CONFIRM_WAIT + 60)]},
        )
        last_text = ""
        async with ClaudeSDKClient(options=options) as client:
            self.clients[s.num] = client
            await client.query(self._prompt(job))
            results: list[str] = []
            seen_injected = 0
            after_result = False
            stream = client.receive_messages().__aiter__()
            while True:
                try:
                    # 插过话又出了结果：再等几秒，插的话若没赶上这一轮，它会接着开一轮，要接着读完
                    msg = await (asyncio.wait_for(stream.__anext__(), 4) if after_result else stream.__anext__())
                except (StopAsyncIteration, asyncio.TimeoutError):
                    break
                after_result = False
                if isinstance(msg, SystemMessage):
                    if "compact" in msg.subtype:
                        turn.compacted = True
                    elif msg.subtype == "init" and not turn.model:
                        turn.model = str(msg.data.get("model") or "")
                elif isinstance(msg, AssistantMessage):
                    if msg.model:
                        turn.model = msg.model
                    for b in msg.content:
                        if isinstance(b, ThinkingBlock) and b.thinking.strip():
                            turn.timeline.append({"t": time.time(), "kind": "think", "text": b.thinking[:4000]})
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock) and b.text.strip()]
                    if texts:
                        last_text = "\n".join(texts)
                        turn.timeline.append({"t": time.time(), "kind": "say", "text": last_text[:6000]})
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            if label := step_label(b.name, b.input or {}):
                                turn.steps.append(label)
                                turn.timeline.append({"t": time.time(), "kind": "do", "text": label,
                                                      "detail": _tool_detail(b.name, b.input or {})})
                            if b.name in EDIT_TOOLS:
                                fp = b.input.get("file_path") or b.input.get("notebook_path")
                                if fp:
                                    turn.changed_files.append(fp)
                            elif b.name == "Bash":
                                turn.commands.append(str(b.input.get("command", ""))[:200])
                elif isinstance(msg, ResultMessage):
                    turn.accepting = False
                    if msg.session_id:
                        s.claude_id = msg.session_id
                    if msg.is_error and not turn.interrupted:
                        turn.is_error = True
                        turn.error = (msg.result or "; ".join(msg.errors or []) or msg.subtype or "未知错误")[:300]
                    elif msg.result:
                        last_text = msg.result
                        results.append(msg.result)
                    if turn.injected > seen_injected and not turn.interrupted:
                        seen_injected = turn.injected
                        after_result = True
                        continue
                    break
            try:
                usage = await client.get_context_usage()
                turn.context_pct = s.context_pct = float(usage["percentage"])
            except Exception:  # noqa: BLE001 - 拿不到上下文用量不影响报告
                log.debug("取上下文用量失败", exc_info=True)
        turn.final_text = "\n\n---\n\n".join(dict.fromkeys(results)) if len(results) > 1 else last_text
        turn.effort = s.effort or self.default_effort


def _tool_detail(name: str, inp: dict) -> str:
    """过程页里一步的细节：命令原文、文件路径、搜索词等。"""
    if name == "Bash":
        return str(inp.get("command", ""))[:3000]
    for key in ("file_path", "notebook_path", "pattern", "query", "url", "description", "skill"):
        if inp.get(key):
            return str(inp[key])[:500]
    return json.dumps(inp, ensure_ascii=False)[:500]


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


COMPACT = "/compact"  # Claude Code 的压缩上下文指令，作为一句话发给对话即可


def _request_title(job: Job) -> str:
    if job.text == COMPACT:
        return "压缩上下文"
    return brief(job.text) or ("看图" if job.images else "")


def describe(s: Session, running: bool) -> str:
    state = {"idle": "空闲", "running": "在做", "queued": "排队", "error": "上次出错"}.get(s.status, s.status)
    if running:
        state = "在做"
    extra = f" ＋{len(s.add_dirs)} 个文件夹" if s.add_dirs else ""
    ctx = f"　上下文 {s.context_pct:.0f}%" if s.context_pct is not None else ""
    return f"#{s.num} {s.name}　{state}　{display(Path(s.cwd))}{extra}{ctx}"
