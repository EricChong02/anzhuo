"""对话登记与运行：每个对话一个编号，背后是一个 Claude Code 对话；几个对话可同时干活，超过上限的排队。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, StreamEvent, SystemMessage, TextBlock,
    ThinkingBlock, ToolUseBlock, create_sdk_mcp_server, tool,
)
from claude_agent_sdk.types import TaskNotificationMessage, TaskProgressMessage, TaskStartedMessage, TaskUpdatedMessage

from . import guard
from .folders import blocked, display
from .image_delegate import ImageDelegator
from .cards import step_label
from .report import Turn, brief

log = logging.getLogger(__name__)

STALE = 7 * 86400  # 7 天没动的对话自动收起：不进列表
CHAT_STALE = 86400  # 只聊天、没动过文件的对话，一天没动就收起（问个天气、查个词，不必在列表里挂一周）

# 远窗给每个对话追加的固定要求。这几条是远窗与对话之间的“约定”（交图、交文件、列步骤、“要你定”的写法），
# 网页和手机上的按钮、进度条都靠它们工作，所以内置；你自己的习惯和授权写进配置的 extra_instructions，不要改这里。
RULES = """需要AI生图或改图时，调用 generate_images 工具委派 Codex，传清楚提示词、参考图绝对路径及输出目录；不要用占位图冒充生成图。工具成功会自动登记图片附件，不要重复调用 send_images；仍需检查画面是否符合要求；失败应如实说明，不声称已经发出。
做了截图、或有能说明结果的图片时，调用 send_images 工具把图片的绝对路径交出去（最多 6 张）。
需要用户在几个方案里选一个时，先把各方案做成截图并用 send_images 交出，回复第一行写“要你定：”加一句问题，然后列出选项和你的推荐，停下等回复。
选项每个单独一行，写成“A. 一句话说明”“B. …”（最多 4 个）；推荐单独一行，写成“推荐：A，理由”。
用户要文件（PDF、文档、表格、原件等）时，用 send_files 把文件原样交给用户，不要只发截图；截图只用于看效果。
动手做事、且要多次操作才能完成时（改几处文件、查资料再改、做完再验证等），第一个动作先调用 mcp__anzhuo__update_plan 列出步骤（3—8 步，每步一句 12 字以内的人话，第一步标 doing），之后每开始或做完一步就再调一次更新状态，最后全部标 done 再写回复。用户人在外面，只能靠这份清单知道你做到哪了。只聊天、一两条命令就能答完的不用列。
这一轮回复发出后，本轮里开的后台命令（run_in_background）和 Monitor 都会被停掉，也收不到它们的完成通知。要跑得比这一轮长的活（部署、完整测试、长时间生成），用 nohup … > 日志文件 2>&1 & 脱离启动，回复里写清大约多久、用户下次问时你去看哪个日志；不要说“我盯着、完成后告诉你”。
"""


def build_instructions(language: str = "简体中文", extra: str = "") -> str:
    """内置约定 + 回复语言 + 你自己的追加要求（配置里的 reply_language、extra_instructions）。"""
    lang = f"回复一律用{language}，思考和过程中的说明也用{language}；专有名词、代码、命令、路径保持原样。\n" if language else ""
    return lang + RULES + (extra.strip() + "\n" if extra.strip() else "")

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
    last_summary: str = ""  # 上一轮你说了什么、它答了什么（各一句），列表和通知里用
    from_desktop: bool = False  # 接上的是桌面端开的对话（不是远窗新开的）
    engine: str = "claude"  # claude / codex（用 ChatGPT 自带的 Codex 干活，额度走 ChatGPT 会员）
    codex_file_undo: str = ""
    codex_records: list[str] = field(default_factory=list)  # 分叉继承的轮次记录编号，附件仍用原记录
    codex_pending_hidden: list[str] = field(default_factory=list)
    codex_hidden: list[str] = field(default_factory=list)  # 回退后收起的原分支轮次
    codex_cut_turn: str = ""  # 已核验的原生轮次编号
    codex_id: str | None = None  # Codex 那边的会话编号（接着上次聊）
    pinned: bool = False  # 网页里置顶
    renamed: bool = False  # 你手动改过名：自动起名不再动它
    titled: bool = False  # 已经自动起过短名字
    hidden_at: float = 0.0  # 删除（收起）的时间：30 天内搜得到、能找回
    awaiting_kind: str = ""  # decide = 真在等你定（列了方案、要你批准、直接问你）；suggest = 活做完了、顺口问要不要再做点什么
    worked: bool | None = None  # 动过手（改文件、跑命令、交图交文件）；只聊天的是 False，一天后收起；老对话不知道，按 7 天
    limit_until: float = 0.0  # 撞上 5 小时额度：几点恢复；恢复后一分钟自动接着做（auto_resume 关了就不接）
    auto_resume: bool = True
    rewind_at: str = ""  # 你点了“回到这里”：下一句从这条记录接着（之后的收起）；还没说下一句前能撤销
    rewind_note: str = ""  # 回到这里时顺带说明（收起了几条、文件退没退）
    fork_from: int = 0  # 从哪个对话分叉出来的（编号）
    forking: bool = False  # 分叉出来还没说第一句：下一句从 rewind_at 那条接着、另起一个 Claude 对话（原对话不动）

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
        known = set(Session.__dataclass_fields__)
        self.sessions = {int(k): Session(**{f: x for f, x in v.items() if f in known}) for k, v in raw.get("sessions", {}).items()}
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
        """没被收起的对话：没删、7 天内动过（只聊天的 1 天）；正在做、排队、在等你的总算在内。"""
        now = time.time()
        return [s for s in self.sessions.values()
                if not s.hidden and (s.pinned or s.status in ("running", "queued")
                                     or (s.awaiting_kind == "decide" and now - s.updated < STALE)
                                     or now - s.updated < (CHAT_STALE if s.worked is False else STALE))]


class FolderBlocked(Exception):
    """要用的文件夹打不开：多半是 macOS 隐私保护在等你点“允许”。"""


@dataclass
class Job:
    schedule_id: str = field(default="", kw_only=True)
    schedule_run_id: str = field(default="", kw_only=True)
    text: str
    images: list[Path] = field(default_factory=list)
    finished: bool = False
    via: str = ""  # 从哪说的（web / dingtalk / schedule / auto）
    parts: list[str] = field(default_factory=list)  # 还没开工时你连着说的几句并成了这一件（网页上照旧一句一句显示）


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
        self.image_delegate = ImageDelegator(registry.path.parent / 'image_jobs') if registry else None
        self.image_tasks: dict[int, set[asyncio.Task]] = {}
        self.codex_live: dict = {}  # 正在跑的 Codex 连接（插话、停下用）
        self.limits_gpt: dict[str, tuple[float, str | None]] = {}  # ChatGPT 会员的 Codex 额度（同上格式）
        self._gpt_usage_at = 0.0
        self.last: dict[int, Turn] = {}  # 每个对话刚做完的那一轮（记录写好前，网页先用它显示）
        self.turns: dict[int, Turn] = {}
        self.limits: dict[str, tuple[float, str | None]] = {}  # 额度：类型 → (已用比例, 何时重置)
        self._usage_at = 0.0
        self.default_effort = default_effort or user_setting("effortLevel") or ""
        self.language = "简体中文"   # 回复语言（配置 reply_language）
        self.extra_instructions = ""   # 你自己追加给每个对话的要求（配置 extra_instructions）
        self.auto_review = False
        self.review_policy = ""   # 配置 review_policy：你额外授权审核员放行的事
        self.auto_title = False   # 头一轮做完自动起短名字（app 里打开；测试里不调模型）  # 危险命令交给审核员（reviewer.py）判断；关掉就照旧等你点

    def busy(self, num: int) -> bool:
        return num in self.turns or not self.queues.get(num, asyncio.Queue()).empty()

    def submit(self, s: Session, job: Job) -> int:
        """这个对话正在做：直接插话给它（返回 -1），它手上这一步做完就看到；否则排队，返回前面还排着几件。"""
        turn, client = self.turns.get(s.num), self.clients.get(s.num)
        if (s.engine == "codex" and turn and turn.accepting and not turn.interrupted and s.num in self.codex_live
                and job.text != COMPACT and turn.request != _request_title(Job(text=COMPACT))):
            # Codex 干活中途插话：交给正在进行的这一轮（turn/steer），插不进去就排到下一轮
            turn.injected += 1
            turn.timeline.append({"t": time.time(), "kind": "you", "text": job.text[:3000]})
            asyncio.create_task(self._steer_codex(s, job))
            return -1
        # 正在压缩的那一轮不插话（插进去可能被压缩吞掉），排到压完之后
        if (turn and client and s.engine != "codex" and turn.accepting and not turn.interrupted and job.text != COMPACT
                and turn.request != _request_title(Job(text=COMPACT))):
            turn.injected += 1
            turn.timeline.append({"t": time.time(), "kind": "you", "text": job.text[:3000]})
            asyncio.create_task(self._inject(s, client, job))
            return -1
        if (turn and not client and s.engine != "codex" and turn.accepting and not turn.interrupted and job.text != COMPACT
                and turn.request != _request_title(Job(text=COMPACT))):
            # 刚开工、Claude 还没连上（头几秒）：等它连上就插进这一轮，不排到下一轮单独再答一遍
            turn.injected += 1
            turn.timeline.append({"t": time.time(), "kind": "you", "text": job.text[:3000]})
            asyncio.create_task(self._inject_when_ready(s, turn, job))
            return -1
        q = self.queues.setdefault(s.num, asyncio.Queue())
        ahead = q.qsize() + (1 if s.num in self.turns else 0)
        last = q._queue[-1] if q.qsize() else None  # noqa: SLF001
        if last and last.text != COMPACT and job.text != COMPACT and last.via == job.via:
            # 前一句还排着没开工，你又补了一句：并成一件交给它，免得它把几句话分成几轮一句句答
            last.parts = last.parts or [last.text]
            last.parts.append(job.text)
            last.text = f"{last.text}\n\n{job.text}"
            last.images += job.images
            return ahead - 1
        q.put_nowait(job)
        if s.num not in self.workers or self.workers[s.num].done():
            self.workers[s.num] = asyncio.create_task(self._worker(s.num))
        return ahead

    async def _steer_codex(self, s: Session, job: Job) -> None:
        try:
            await self.codex_live[s.num].steer(job.text, job.images)
            job.finished = True
        except Exception:  # noqa: BLE001 - 这一轮刚好结束、插不进去：排到下一轮
            log.warning("插话给 Codex #%s 失败，排到下一轮", s.num, exc_info=True)
            q = self.queues.setdefault(s.num, asyncio.Queue())
            q.put_nowait(job)
            if s.num not in self.workers or self.workers[s.num].done():
                self.workers[s.num] = asyncio.create_task(self._worker(s.num))

    async def _inject_when_ready(self, s: Session, turn: Turn, job: Job) -> None:
        for _ in range(180):
            if self.turns.get(s.num) is not turn or not turn.accepting:
                break
            if client := self.clients.get(s.num):
                return await self._inject(s, client, job)
            await asyncio.sleep(0.5)
        q = self.queues.setdefault(s.num, asyncio.Queue())   # 没赶上这一轮：排到下一轮
        q.put_nowait(job)
        if s.num not in self.workers or self.workers[s.num].done():
            self.workers[s.num] = asyncio.create_task(self._worker(s.num))

    async def _inject(self, s: Session, client: ClaudeSDKClient, job: Job) -> None:
        try:
            await client.query(self._prompt(job))
            job.finished = True
        except Exception:  # noqa: BLE001 - 插不进去就排到下一轮
            log.warning("插话给 #%s 失败，排到下一轮", s.num, exc_info=True)
            q = self.queues.setdefault(s.num, asyncio.Queue())
            q.put_nowait(job)

    def queued(self, num: int) -> list[Job]:
        """还排着没开始的（按顺序）。"""
        q = self.queues.get(num)
        return list(q._queue) if q else []  # noqa: SLF001 - asyncio.Queue 没有公开的查看方法

    def unqueue_last(self, num: int) -> Job | None:
        """撤回还排着没开始的最后一句。"""
        q = self.queues.get(num)
        if not q or q.empty():
            return None
        job = q._queue.pop()  # noqa: SLF001
        return job

    async def rewind_files(self, s: Session, user_uuid: str) -> str:
        """把文件退回到你说某句话之前的样子（Claude 自己的改动记录，只有开了记录之后的轮次有）。返回空 = 成功，否则是原因。"""
        options = ClaudeAgentOptions(cwd=s.cwd, add_dirs=s.add_dirs, resume=s.claude_id, setting_sources=[],
                                     permission_mode="bypassPermissions", enable_file_checkpointing=True,
                                     extra_args={"replay-user-messages": None}, stderr=lambda line: log.debug("claude: %s", line))
        client = ClaudeSDKClient(options=options)
        try:
            await asyncio.wait_for(client.connect(), 90)
            await asyncio.wait_for(client.rewind_files(user_uuid), 90)
            return ""
        except Exception as e:  # noqa: BLE001 - 退不回就告诉你原因，对话照样回到那里
            log.warning("#%s 退回文件失败：%s", s.num, e)
            return "文件没退回（没有改动记录）"
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def compacting(self, num: int) -> str:
        """这个对话的压缩：正在压返回 "running"，排着返回 "queued"，没有返回 ""。"""
        turn = self.turns.get(num)
        if turn and turn.request == _request_title(Job(text=COMPACT)):
            return "running"
        return "queued" if any(j.text == COMPACT for j in self.queued(num)) else ""

    def answer_confirm(self, num: int, allow: bool) -> bool:
        """你在手机 / 钉钉 / 网页上点了允许或不允许。"""
        turn = self.turns.get(num)
        if not turn or not turn.confirm or turn.confirm["fut"].done():
            return False
        turn.confirm["fut"].set_result(allow)
        return True

    async def ask_confirm(self, s: Session, turn: Turn, what: str, cmd: str) -> bool:
        """危险操作停下来等你点头（手机通知 + 钉钉按钮 + 网页卡片）；20 分钟不回当不允许。Claude 和 Codex 共用。"""
        fut = asyncio.get_running_loop().create_future()
        turn.confirm = {"what": what, "cmd": cmd[:600], "fut": fut}
        turn.timeline.append({"t": time.time(), "kind": "do", "text": f"等你确认：{what}", "detail": cmd[:400]})
        if self.on_confirm:
            asyncio.create_task(self.on_confirm(s, turn))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), CONFIRM_WAIT)
        except asyncio.TimeoutError:
            return False
        finally:
            turn.confirm = None

    async def decide(self, s: Session, turn: Turn, what: str, cmd: str) -> bool:
        """危险命令：开了审核员（auto_review）就交给它判断，做完在报告里告诉你；它出错或超时才退回来问你。"""
        if self.auto_review:
            from .reviewer import review
            recent = [t["text"] for t in turn.timeline if t.get("kind") in ("do", "say")]
            r = await review(what, cmd, s.cwd, turn.request, recent, policy=self.review_policy)
            if r is not None:
                ok, why = r
                turn.reviews.append({"allow": ok, "what": what, "why": why, "cmd": cmd[:300]})
                turn.timeline.append({"t": time.time(), "kind": "do", "text": f"审核员{'放行' if ok else '拦下'}：{what}（{why}）",
                                      "detail": cmd[:400]})
                return ok
        return await self.ask_confirm(s, turn, what, cmd)

    def _guard(self, s: Session, turn: Turn):
        async def hook(data, tool_use_id, context):
            cmd = str((data.get("tool_input") or {}).get("command") or "")
            what = guard.danger(cmd)
            if not what:
                return {}
            allow = await self.decide(s, turn, what, cmd)
            why = turn.reviews[-1]["why"] if turn.reviews and turn.reviews[-1]["cmd"] == cmd[:300] else ""
            out = {"hookEventName": "PreToolUse"}
            if allow:
                out.update(permissionDecision="allow", permissionDecisionReason="审核通过，可以执行")
            else:
                out.update(permissionDecision="deny", permissionDecisionReason=(
                    f"这一步没有通过审核{('（' + why + '）') if why else ''}。不要执行它，也不要换个写法绕过去；"
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
        image_tasks = list(self.image_tasks.get(num, ()))
        for task in image_tasks:
            task.cancel()
        if image_tasks:
            await asyncio.gather(*image_tasks, return_exceptions=True)
        if conn := self.codex_live.get(num):
            try:
                await conn.interrupt()
            except Exception:  # noqa: BLE001 - 停不下来就直接关掉
                await conn.close()
            return True
        if client:
            await client.interrupt()
            return True
        return bool(image_tasks)

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
                turn = Turn(started=time.time(), request=_request_title(job), via=job.via, last_at=time.time(), schedule_id=job.schedule_id, schedule_run_id=job.schedule_run_id)
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
                    turn.error = (f"打不开 {e}。下载、桌面、文稿这几个文件夹受 macOS 隐私保护，远窗在后台头一次用时，"
                                  "Mac 会弹窗等人点“允许”，没人点就卡住。回到电脑前给远窗授权一次就好（做法见说明的“文件夹权限”）。")
                except Exception as e:  # noqa: BLE001 - 任何失败都要报给用户
                    log.exception("对话 #%s 运行失败", num)
                    turn.is_error = True
                    turn.error = f"{type(e).__name__}: {str(e)[:300]}"
                    if "initialize" in turn.error:
                        turn.error = "Claude 一分钟内没启动起来（常见原因：Mac 在等权限弹窗，或网络不通）。原始信息：" + turn.error
                finally:
                    image_tasks = list(self.image_tasks.get(s.num, ()))
                    for task in image_tasks:
                        task.cancel()
                    if image_tasks:
                        await asyncio.gather(*image_tasks, return_exceptions=True)
                    turn.ended = time.time()
                    self.last[num] = turn
                    self.turns.pop(num, None)
                    self.clients.pop(num, None)
                    if (turn.is_error and s.claude_id == started_before and not turn.final_text
                            and not re.search(r"hit your .{0,20}limit", turn.error)):   # 撞额度时它其实收到了，恢复后说“继续”就行
                        s.unsent = job.text  # Claude 一个字都没收到，下次一起带上
                    s.status = "error" if turn.is_error else "idle"
                    s.updated = time.time()
                    if turn.changed_files or turn.commands or turn.images or turn.files:
                        s.worked = True
                    elif s.worked is None and started_before is None and not turn.is_error:
                        s.worked = False   # 新开的对话头一轮只聊了天
                    self.registry.save()
            await (self.refresh_gpt_usage() if s.engine == "codex" else self.refresh_usage())
            job.finished = True
            if self.auto_title and not (s.renamed or s.titled or turn.is_error or job.text == COMPACT):
                asyncio.create_task(self._auto_title(s, job.text, turn.final_text))
            await self.on_done(s, turn)

    async def title_all(self, records_dir: Path) -> None:
        """还没起过短名字的对话（你手动改过名的除外）一个个补上：按它最近一轮你说的话和回复起。启动时跑一次。"""
        for s in list(self.registry.sessions.values()):
            from .naming import valid
            if s.renamed or (s.titled and valid(s.name)) or s.status in ("running", "queued"):
                continue   # 早先起的长名字、起坏了的（“Here's a tit”“大会话名字：…”）也重起一次
            recs = sorted(records_dir.glob(f"{s.num}-*.json"), key=lambda p: int(p.stem.split("-")[1]))
            try:
                first = json.loads(recs[0].read_text()) if recs else {}
                last = json.loads(recs[-1].read_text()) if recs else {}
            except (OSError, json.JSONDecodeError):
                continue
            asked = "\n".join(x for x in (first.get("request"), last.get("request")) if x) or s.name
            await self._auto_title(s, asked, last.get("final_text") or s.last_summary)

    async def _auto_title(self, s: Session, asked: str, answer: str) -> None:
        """头一轮做完，按“你要做的事 + 它的回复”起个短名字（原来是你第一句话的开头，长、看不出在做什么）。"""
        from .naming import short_title
        if (t := await short_title(asked, answer)) and not s.renamed:
            s.name, s.titled = t, True
            self.registry.save()

    async def refresh_gpt_usage(self, force: bool = False) -> None:
        """Codex（GPT）的额度：开个连接读一下，2 分钟内不重复读。"""
        if not force and time.time() - self._gpt_usage_at < 120:
            return
        self._gpt_usage_at = time.time()
        from .codex import rate_limits
        if got := await rate_limits():
            self.limits_gpt = got

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

    async def _generate_images(self, s: Session, turn: Turn, args: dict) -> dict:
        if self.image_delegate is None:
            self.image_delegate = ImageDelegator(self.registry.path.parent / 'image_jobs')

        def progress(job_id, state, text):
            key = 'image:' + job_id
            if key not in turn.agents:
                turn.steps.append('委派 Codex 生图')
                turn.timeline.append({'t': time.time(), 'kind': 'do', 'text': '委派 Codex 生图', 'detail': job_id})
            turn.last_at = time.time()
            agent = turn.agents.setdefault(key, {'name': 'Codex 生图', 'kind': 'agent', 't0': time.time(), 't1': 0})
            if agent.get('last') and agent['last'] != text and state in ('queued', 'running'):
                turn.timeline.append({'t': time.time(), 'kind': 'do', 'text': text, 'detail': job_id})
            agent.update(status='running' if state in ('queued', 'running') else 'stopped' if state in ('cancelled', 'timeout') else state, last=text)
            if state not in ('queued', 'running'):
                agent['t1'] = time.time()
                turn.timeline.append({'t': time.time(), 'kind': 'do', 'text': text, 'detail': job_id})

        async def approve(method, params):
            if method in ('item/commandExecution/requestApproval', 'execCommandApproval'):
                cmd = params.get('command') or ''
                cmd = ' '.join(cmd) if isinstance(cmd, list) else str(cmd)
                ok = not turn.interrupted
                if ok and (what := guard.danger(cmd)):
                    ok = await self.decide(s, turn, what, cmd)
                return {'decision': ('approved' if ok else 'denied') if method == 'execCommandApproval' else ('accept' if ok else 'decline')}
            if method in ('item/fileChange/requestApproval', 'applyPatchApproval'):
                return {'decision': ('approved' if not turn.interrupted else 'denied') if method == 'applyPatchApproval' else ('accept' if not turn.interrupted else 'decline')}
            return None

        task = asyncio.create_task(self.image_delegate.run(prompt=args.get('prompt', ''), references=args.get('reference_images') or [],
                cwd=s.cwd, output_dir=args.get('output_dir') or '', add_dirs=s.add_dirs, parent_num=s.num, progress=progress, approve=approve))
        tasks = self.image_tasks.setdefault(s.num, set())
        tasks.add(task)
        try:
            result = await task
            if turn.interrupted:
                return {'state': 'cancelled', 'images': [], 'error': '原任务已停止，未登记附件'}
            if result['state'] == 'completed':
                for p in result['images']:
                    if p not in turn.images:
                        turn.images.append(p)
                    if p not in turn.changed_files:
                        turn.changed_files.append(p)
                result['message'] = '真实图片已保存并登记到本轮附件，原对话完成后显示；需要检查画面请用 Read 查看图片。此回执不代表用户已收到。'
            return result
        except asyncio.CancelledError:
            if turn.interrupted:
                return {'state': 'cancelled', 'images': [], 'error': '原任务已停止，Codex 委派连接已关闭'}
            raise
        finally:
            tasks.discard(task)
            if not tasks:
                self.image_tasks.pop(s.num, None)

    def _image_tool(self, turn: Turn, session: Session | None = None):
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
            msg = f"已登记 {len(ok)} 张图片，本轮结束后会显示在对话中；这不是用户已收到的回执。"
            if bad:
                msg += f" 没收下：{', '.join(bad)}（不存在、不是图片或超过 6 张）"
            return {"content": [{"type": "text", "text": msg}]}

        @tool("send_files", "把文件原样交给用户（PDF、Word、表格、压缩包等任何格式，用户能直接下载）。paths 是本机文件的绝对路径，最多 10 个，每个不超过 300MB。"
              "用户要的是文件本身时用这个，不要只发截图。",
              {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
               "required": ["paths"]})
        async def send_files(args):
            ok, bad = [], []
            for p in args.get("paths", []):
                path = Path(p).expanduser()
                if path.is_file() and path.stat().st_size <= 300 * 1024 * 1024 and len(turn.files) + len(ok) < 10:
                    ok.append(str(path))
                else:
                    bad.append(p)
            turn.files.extend(ok)
            msg = f"已登记 {len(ok)} 个文件，本轮结束后会显示下载入口；这不是用户已收到的回执。"
            if bad:
                msg += f" 没收下：{', '.join(bad)}（不存在、超过 300MB 或超过 10 个）"
            return {"content": [{"type": "text", "text": msg}]}

        @tool("update_plan", "列出或更新这件事的步骤清单，用户在手机上看进度。每次传完整清单；status 只能是 pending（没开始）、doing（正在做）、done（做完）。",
              {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "object", "properties": {
                  "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "doing", "done"]}},
                  "required": ["text", "status"]}}}, "required": ["steps"]})
        async def update_plan(args):
            turn.plan = [{"text": str(x.get("text", ""))[:60], "status": x.get("status") if x.get("status") in ("pending", "doing", "done") else "pending"}
                         for x in (args.get("steps") or [])[:20]]
            left = [x["text"] for x in turn.plan if x["status"] != "done"]
            tip = f"已更新：做完 {len(turn.plan) - len(left)}/{len(turn.plan)} 步。" + (
                f"每做完一步就马上再调一次 update_plan，把它标 done、下一步标 doing（还剩：{'、'.join(left)}）。" if left else "全部做完了。")
            return {"content": [{"type": "text", "text": tip}]}

        @tool("generate_images", "委派 Codex 使用内置生图工具生成或编辑图片，等待真实文件结果并自动登记为本轮图片附件。仅用于图片任务，不能代替一般代码任务；参考图传绝对路径，输出目录应在当前工作目录或附加目录内。成功后用 Read 检查图片再继续。",
              {"type": "object", "properties": {"prompt": {"type": "string"}, "reference_images": {"type": "array", "items": {"type": "string"}}, "output_dir": {"type": "string"}}, "required": ["prompt"]})
        async def generate_images(args):
            if session is None or turn.interrupted:
                result = {"state": "cancelled", "images": [], "error": "当前任务不可委派"}
            else:
                try:
                    result = await self._generate_images(session, turn, args)
                except (ValueError, OSError) as exc:
                    result = {"state": "failed", "images": [], "error": str(exc)[:500]}
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": result["state"] != "completed"}

        return create_sdk_mcp_server("anzhuo", tools=[send_images, send_files, update_plan, generate_images])

    async def _prompt(self, job: Job):
        content: list[dict] = []
        for p in job.images:
            mt = mimetypes.guess_type(p.name)[0] or "image/png"
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                         "data": base64.b64encode(p.read_bytes()).decode()}})
        if job.images:
            content.append({"type": "text", "text": "本轮参考图片的本机路径（按显示顺序，供图片工具引用）：\n" +
                            "\n".join(f"{i}. {p.resolve()}" for i, p in enumerate(job.images, 1))})
        content.append({"type": "text", "text": job.text or "（见图）"})
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    async def _run(self, s: Session, job: Job, turn: Turn) -> None:
        if s.engine == "codex":
            return await _codex_run(self, s, job, turn)
        if stuck := await asyncio.to_thread(blocked, [Path(s.cwd), *map(Path, s.add_dirs)]):
            raise FolderBlocked("、".join(display(p) for p in stuck))
        options = ClaudeAgentOptions(
            cwd=s.cwd,
            add_dirs=s.add_dirs,
            model=s.model,
            effort=s.effort or self.default_effort or None,
            resume=s.claude_id,
            resume_session_at=(s.rewind_at or None) if s.claude_id else None,   # 你点过“回到这里”：从那条接着，后面的不带上
            fork_session=bool(s.forking and s.claude_id),   # 分叉：从原对话那条接着，但另起一个对话编号，原对话不受影响
            enable_file_checkpointing=True,   # 记下每轮改了哪些文件，“回到这里”时能把文件一起退回
            setting_sources=["user", "project", "local"],
            permission_mode="bypassPermissions",
            system_prompt={"type": "preset", "preset": "claude_code", "append": build_instructions(self.language, self.extra_instructions)},
            mcp_servers={"anzhuo": self._image_tool(turn, s)},
            env={"ENABLE_TOOL_SEARCH": "false"},   # 交图、交文件、步骤清单这几个工具一开始就给它，不用先搜（搜到了它也常忘了用）
            stderr=lambda line: log.debug("claude: %s", line),
            max_buffer_size=64 * 1024 * 1024,  # 默认 1MB：Claude 看一张大截图就超，整轮报错（10-25 #6 实际遇到）
            thinking={"type": "adaptive", "display": "summarized"},
            include_partial_messages=True,  # 回复边写边出（网页实时看）  # 要思考摘要（默认不给字），网页“工作过程”里看它怎么想的
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
            wait_more = 4
            stream = client.receive_messages().__aiter__()
            while True:
                try:
                    # 插过话又出了结果：再等几秒，插的话若没赶上这一轮，它会接着开一轮，要接着读完
                    msg = await (asyncio.wait_for(stream.__anext__(), wait_more) if after_result else stream.__anext__())
                except (StopAsyncIteration, asyncio.TimeoutError):
                    break
                after_result = False
                turn.last_at = time.time()
                if isinstance(msg, (TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage, TaskUpdatedMessage)):
                    track_agent(turn, msg)
                elif isinstance(msg, SystemMessage):
                    if "compact" in msg.subtype:
                        turn.compacted = True
                    elif msg.subtype == "init" and not turn.model:
                        turn.model = str(msg.data.get("model") or "")
                elif isinstance(msg, StreamEvent):
                    # 边写边看：只接主对话的正文增量（思考多是英文，等写完译好再出，免得读着换字）
                    ev = msg.event or {}
                    if msg.parent_tool_use_id is None:
                        if ev.get("type") == "content_block_start":
                            turn.partial = ""
                        elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                            turn.partial = (turn.partial + ev["delta"].get("text", ""))[-6000:]
                elif isinstance(msg, AssistantMessage):
                    turn.partial = ""
                    if msg.model:
                        turn.model = msg.model
                    for b in msg.content:
                        if isinstance(b, ThinkingBlock) and b.thinking.strip():
                            turn.timeline.append({"t": time.time(), "kind": "think", "text": b.thinking[:4000]})
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock) and b.text.strip()]
                    if texts:
                        last_text = "\n".join(texts)
                        turn.timeline.append({"t": time.time(), "kind": "say", "text": last_text[:6000]})
                    sub = sub_name(turn, msg.parent_tool_use_id)
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            if label := step_label(b.name, b.input or {}):
                                turn.steps.append(label)
                                e = {"t": time.time(), "kind": "do", "text": label, "detail": _tool_detail(b.name, b.input or {})}
                                if sub:
                                    e["sub"] = sub   # 助手做的步骤：过程里缩进、标上是哪个助手
                                turn.timeline.append(e)
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
                        s.rewind_at = s.rewind_note = ""   # 已经从那条接着说了，回到这里落定
                        s.forking = False   # 分叉出来的已经有了自己的对话编号
                    if not msg.is_error and NOISE.match(msg.result or ""):
                        # 上一轮留下的后台任务通知先被答了一句“No response requested.”，你这句还排在后面：接着读，别当成回复
                        after_result, wait_more = True, 60
                        continue
                    if running_agents(turn) and not turn.interrupted and not msg.is_error:
                        # 主对话先说完了，但还有助手在后台跑：这一轮一结束它们就被关掉，所以接着等，助手做完它会接着汇总
                        after_result, wait_more = True, AGENT_WAIT
                        continue
                    wait_more = 4
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


AGENT_WAIT = 20 * 60   # 主对话说完后，最多再等后台助手这么久
DONE = {"completed", "failed", "stopped", "killed", "error", "cancelled"}


def track_agent(turn, msg) -> None:
    """记下派出去的助手：开始、做到哪一步、结束。网页“在做”卡片和过程里按这个显示。"""
    a = turn.agents.get(msg.task_id)
    if isinstance(msg, TaskStartedMessage):
        kind = "命令" if "bash" in (msg.task_type or "") else "助手"
        turn.agents[msg.task_id] = {"name": _short_desc(msg.description), "kind": kind, "t0": time.time(), "t1": 0,
                                    "status": "running", "last": "", "tool": msg.tool_use_id}
        return
    if a is None:
        return
    if isinstance(msg, TaskProgressMessage):
        if msg.last_tool_name:
            a["last"] = (step_label(msg.last_tool_name, {}) or "").rstrip("：")
        return
    status = msg.status if isinstance(msg, TaskNotificationMessage) else (msg.status or (msg.patch or {}).get("status"))
    if status in DONE and a["status"] == "running":
        a["status"], a["t1"] = ("done" if status == "completed" else "failed" if status in ("failed", "error") else "stopped"), time.time()


def running_agents(turn) -> list[dict]:
    # 只等子代理；后台命令（开着的预览服务、Monitor 之类）可能永远不结束，等它会把对话卡住、你说的话一直排队
    return [a for a in turn.agents.values() if a["status"] == "running" and a.get("kind") == "助手"]


def sub_name(turn, parent_id) -> str:
    if not parent_id:
        return ""
    return next((a["name"] for a in turn.agents.values() if a.get("tool") == parent_id), "助手")


def _short_desc(t: str) -> str:
    t = " ".join(str(t or "").split())
    return t if len(t) <= 16 else t[:15] + "…"


NOISE = re.compile(r"^\s*(No response requested\.?)?\s*$", re.I)   # 空结果也算：后台通知有时连一句话都不答就出结果
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# 回复里写成下载链接的本机文件，这几类当作要交给你的（源码、说明文档之类的链接只是出处，不发）
GIVE_EXT = IMG_EXT + (".zip", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".mp4", ".mov", ".mp3", ".7z", ".tar", ".gz")
LINK = re.compile(r"\[([^\]]+)\]\((/[^)\s]+|~/[^)\s]+|[A-Za-z]:[\\/][^)\s]+)\)")


def handoff(text: str, cwd: str) -> tuple[str, list[str], list[str]]:
    """GPT 回复里要交给你的截图和文件：最后一行“交给你：路径”；它常常只写成 [下载…](/本机/路径) 链接，
    这种链接你在手机上点不开，指向能交的文件就拿出来照常发（网页、钉钉），链接只留文字。"""
    imgs: list[str] = []
    files: list[str] = []

    def take(raw: str) -> bool:
        fp = Path(raw.strip("`'\"")).expanduser()
        if not fp.is_absolute():
            fp = Path(cwd) / fp
        if not fp.is_file() or str(fp) in imgs + files:
            return fp.is_file()
        (imgs if fp.suffix.lower() in IMG_EXT else files).append(str(fp))
        return True

    keep = []
    for line in text.splitlines():
        m = re.match(r"^\s*交给你[:：]\s*(.+)$", line)
        if m:
            for raw in re.split(r"[,，、;；\s]+", m.group(1)):
                if raw:
                    take(raw)
            continue
        keep.append(LINK.sub(lambda m: f"**{m.group(1)}**" if Path(m.group(2)).suffix.lower() in GIVE_EXT and take(m.group(2))
                             else m.group(0), line))
    return "\n".join(keep).strip(), imgs, files


CODEX_RULES = ("需要用户在几个方案里选时，第一行写“要你定：”加一句问题，选项写成“A. …”，推荐写成“推荐：A，理由”。"
               "要把截图或文件（PDF、表格等）交给用户看，在回复最后单独一行写“交给你：”加文件的绝对路径（多个用顿号隔开）；"
               "用户在手机上看，点不开本机路径，不要只写成 Markdown 下载链接。")


def codex_instructions(language: str = "简体中文", extra: str = "") -> str:
    lang = f"回复一律用{language}；专有名词、代码、命令、路径保持原样。" if language else ""
    return (lang + CODEX_RULES + (" " + extra.strip() if extra.strip() else "")).strip()



async def _codex_run(self, s: Session, job: Job, turn: Turn) -> None:
    """用 Codex 跑一轮（常驻连接 app-server）：开或接着会话 → 开一轮 → 把事件翻成和 Claude 一样的步骤、说明、回复；
    正在写的回复边写边给网页；干活中途你说的话用 turn/steer 插进这一轮（见 Runner.submit）。
    只能写对话所在的文件夹（workspace-write，可联网）。"""
    from .codex import CodexConn, CodexError
    if stuck := await asyncio.to_thread(blocked, [Path(s.cwd), *map(Path, s.add_dirs)]):
        raise FolderBlocked("、".join(display(p) for p in stuck))
    from .checkpoints import Checkpoints
    checkpoints = Checkpoints(self.registry.path.parent / "checkpoints")
    if job.text != COMPACT:
        try:
            turn.checkpoint_before = await asyncio.to_thread(checkpoints.capture, [s.cwd, *s.add_dirs])
        except OSError:
            log.warning("#%s 文件快照未建立", s.num)
    effort = s.effort or self.default_effort or "medium"
    texts: list[str] = []
    started_cmds: set[str] = set()

    def on_event(method: str, prm: dict) -> None:
        turn.last_at = time.time()
        item = prm.get("item") or {}
        kind = item.get("type")
        if method == "item/agentMessage/delta":
            turn.partial = (turn.partial + str(prm.get("delta") or ""))[-6000:]
        elif method == "item/started" and kind == "commandExecution":
            cmd = str(item.get("command") or "")
            cmd = re.sub(r"^/bin/(?:zsh|bash) -lc ", "", cmd).strip("'\"")
            started_cmds.add(item.get("id") or cmd)
            label = "跑命令：" + brief(cmd, 18)
            turn.steps.append(label)
            turn.commands.append(cmd[:200])
            turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": cmd[:3000]})
        elif method == "item/completed" and kind == "agentMessage" and (item.get("text") or "").strip():
            turn.partial = ""
            texts.append(item["text"].strip())
            turn.timeline.append({"t": time.time(), "kind": "say", "text": item["text"][:6000]})
        elif method == "item/completed" and kind == "reasoning":
            summ = item.get("summary") or []
            text = "\n".join(x if isinstance(x, str) else str(x.get("text", "")) for x in summ).strip()
            if text:
                turn.timeline.append({"t": time.time(), "kind": "think", "text": text[:4000]})
        elif method == "item/completed" and kind == "fileChange":
            for ch in item.get("changes") or []:
                fp = ch.get("path") if isinstance(ch, dict) else None
                if fp:
                    turn.changed_files.append(fp)
                    label = f"改 {Path(fp).name}"
                    turn.steps.append(label)
                    turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": fp})
        elif method == "item/completed" and kind == "webSearch":
            q = str(item.get("query") or "")
            label = "上网查：" + brief(q, 18)
            turn.steps.append(label)
            turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": q})
        elif method == "turn/plan/updated":   # Codex 自己列的步骤清单
            st = {"completed": "done", "inProgress": "doing", "in_progress": "doing"}
            turn.plan = [{"text": str(x.get("step", ""))[:60], "status": st.get(x.get("status"), "pending")} for x in (prm.get("plan") or [])[:20]]
        elif method == "thread/tokenUsage/updated":
            u = prm.get("tokenUsage") or {}
            win, last = u.get("modelContextWindow"), (u.get("last") or {}).get("totalTokens")
            if win and last:
                turn.context_pct = s.context_pct = min(100.0, 100.0 * last / win)
        elif method == "account/rateLimits/updated":
            from .codex import limits_from
            if got := limits_from(prm.get("rateLimits") or {}):
                self.limits_gpt = got
        elif method == "error":
            msg = str((prm.get("error") or {}).get("message") or prm.get("message") or "")
            if msg and not prm.get("willRetry"):
                turn.error = msg[:300]

    async def on_request(method: str, prm: dict) -> dict | None:
        """Codex 跑命令前来问：危险的交给你点头，其余直接放行（只能写这个文件夹，本来就出不去）。"""
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            cmd = prm.get("command") or ""
            cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            ok = True
            if what := guard.danger(cmd):
                ok = await self.decide(s, turn, what, cmd)
            if method == "execCommandApproval":
                return {"decision": "approved" if ok else "denied"}
            return {"decision": "accept" if ok else "decline"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "accept"}
        if method == "applyPatchApproval":
            return {"decision": "approved"}
        return None

    conn = CodexConn(s.cwd, on_event, on_request)
    self.codex_live[s.num] = conn
    turn.model = "GPT（Codex）"
    turn.timeline.append({"t": time.time(), "kind": "you", "text": (job.text or "（见图）")[:3000]})
    try:
        await conn.start()
        if s.rewind_at:
            if not s.codex_id or not s.codex_cut_turn:
                raise CodexError("找不到回退位置，原对话未改动")
            new_id = await conn.fork_thread(s.codex_id, s.codex_cut_turn)
            s.codex_id = new_id
            s.rewind_at = s.rewind_note = s.codex_cut_turn = ""
            s.forking = False
            s.codex_pending_hidden = []
            s.codex_file_undo = ""
            s.context_pct = None
            self.registry.save()
        else:
            s.codex_id = await conn.open_thread(s.codex_id, codex_instructions(self.language, self.extra_instructions))
        turn.model = f"GPT（{conn.model}）" if conn.model else turn.model
        if job.text == COMPACT:   # 压缩上下文
            before = s.context_pct
            await conn.compact()
            await asyncio.wait_for(conn.done.wait(), 600)
            conn.turn_status = conn.turn_status or "completed"
            turn.final_text = "压缩好了。"
            if before is not None:
                s.context_pct = turn.context_pct = None   # 压完的用量等下一轮再报
        else:
            turn.codex_turn_id = await conn.start_turn(job.text, job.images, effort)
            await conn.done.wait()
    except CodexError as e:
        turn.is_error = True
        turn.error = str(e)[:300]
    finally:
        turn.accepting = False
        self.codex_live.pop(s.num, None)
        await conn.close()
        if turn.checkpoint_before:
            try:
                turn.checkpoint_after = await asyncio.to_thread(checkpoints.capture, [s.cwd, *s.add_dirs])
            except OSError:
                log.warning("#%s 文件结束快照未建立", s.num)
    if conn.turn_status == "failed" and not turn.interrupted:
        turn.is_error = True
        turn.error = (conn.turn_error or turn.error or "Codex 这一轮失败了")[:300]
    elif not conn.turn_status and not turn.is_error and not turn.interrupted:
        turn.is_error = True
        turn.error = ("Codex 中途退出了：" + conn.stderr_tail())[:300]
    turn.partial = ""
    if job.text != COMPACT:
        turn.final_text = texts[-1] if texts else ""
    turn.final_text, imgs, files = handoff(turn.final_text, s.cwd)
    turn.images += imgs
    turn.files += files
    turn.effort = effort


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
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")


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
