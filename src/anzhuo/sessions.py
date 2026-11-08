"""对话登记与调度：每个对话一个编号，背后是一个 Claude Code / Codex / ACP 对话；几个对话可同时干活，超过上限的排队。
怎么跑一轮由引擎负责（engines/），这里只管排队、并行名额、插话与停下的分派、通用收尾。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import engines
from .config import Config
from .engines.base import COMPACT, CONFIRM_WAIT, FolderBlocked  # noqa: F401 - 别处从这里拿
from .engines.codex import COMMAND_APPROVALS, approve_command
from .folders import display
from .image_delegate import ImageDelegator
from .report import Turn, brief

log = logging.getLogger(__name__)

STALE = 7 * 86400  # 7 天没动的对话自动收起：不进列表，远窗分派时也不考虑
CHAT_STALE = 86400  # 只聊天、没动过文件的对话，一天没动就收起（问个天气、查个词，不必在列表里挂一周）
SAID_MAX = 4000  # 每轮记录里存你说的完整的话最多多少字（额度用完接力时拼交接材料用）


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
    context_tokens: int | None = None  # 上下文多少 token（每轮都要把这些重读一遍，长了就提示接力）
    hidden: bool = False  # 你发“删 N”收起的；聊天记录还在，发“#N …”就回来
    unsent: str = ""  # 上一句根本没能开始（比如文件夹打不开、启动超时），下一句带上它，免得 Claude 不知道要做什么
    seen: float = 0.0  # 你最后一次在网页里看这个对话的时间；之后又做完一轮就算“没看的回复”
    awaiting: str = ""  # 上一轮最后在等你回答（问了什么）；你回了话就清掉
    last_summary: str = ""  # 上一轮你说了什么、它答了什么（各一句），给远窗分派时看
    from_desktop: bool = False  # 接上的是桌面端开的对话（不是远窗新开的）
    engine: str = "claude"  # claude / codex（用 ChatGPT 自带的 Codex 干活，额度走 ChatGPT 会员）/ ACP 引擎名（kimi…）；能做什么查 engines/
    codex_file_undo: str = ""
    codex_records: list[str] = field(default_factory=list)  # 分叉继承的轮次记录编号，附件仍用原记录
    codex_pending_hidden: list[str] = field(default_factory=list)
    codex_hidden: list[str] = field(default_factory=list)  # 回退后收起的原分支轮次
    codex_cut_turn: str = ""  # 已核验的原生轮次编号
    codex_id: str | None = None  # Codex 那边的会话编号（接着上次聊）
    acp_id: str = ""  # ACP 引擎（Kimi 等）那边的会话编号（接着上次聊）
    pinned: bool = False  # 网页里置顶
    task_group: str = ""  # 具体任务分组，模型生成或手动指定
    task_group_manual: bool = False  # 手动指定后模型不覆盖
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
    ext: dict = field(default_factory=dict)  # 扩展存在这个对话上的数据：扩展名 → 它自己的字典（见 ext.Api.data）
    owner: str = "admin"  # 哪个网页账号的对话；钉钉里的都算 admin。两个账号互相看不到对方的对话
    root: str = ""  # 受限账号的对话：只能在这个文件夹里干活（空 = 不限，admin 的对话）
    codex_tokens: int = 0  # Codex 这个会话累计用过多少 token（算每轮用了多少）
    claude_usage: dict = field(default_factory=dict)  # Claude 报的这个会话累计金额和各模型 token（接着上次的会话也累计），每轮记差值
    relay_suggested: bool = False  # 已在空闲时建议过接力，之后进行中只显示禁用按钮
    relay_from: int = 0  # 接力：从哪个对话接过来的（编号）
    relay_to: int = 0  # 接力：已经换到哪个新对话
    relay_pending: dict = field(default_factory=dict)  # 接力前尚待回答的问题及原文
    relay_note: str = ""  # 接力过来的交接说明（第一句话前给你看；随第一句话交给它）
    role: str = ""  # router = 总管（super_agent 打开时才有）；空 = 普通对话
    boss: int = 0  # 总管派出来的：总管的编号。它做完总管派的活，先回报给总管
    ticket: str = ""  # 总管派出来的：最近一次汇报的状态（ready / waiting / blocked / failed / dropped / asked / stopped / error）
    notes: list[str] = field(default_factory=list)  # 总管：上次开口后发生的事，下次开口前一起看

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
        if s.owner == "admin":   # “当前对话”是钉钉里直接说话交给谁，只算主账号的
            self.current = s.num
        self.save()
        return s

    def get(self, num: int | None) -> Session | None:
        return self.sessions.get(num) if num is not None else None

    def mine(self, num: int | None, owner: str = "admin") -> Session | None:
        """这个账号的对话（别的账号的当作没有）。"""
        s = self.get(num)
        return s if s and s.owner == owner else None

    def visible(self, owner: str | None = "admin") -> list[Session]:
        """没被收起的对话：没删、7 天内动过（只聊天的 1 天）；正在做、排队、在等你的总算在内。owner=None 不分账号。"""
        now = time.time()
        return [s for s in self.sessions.values()
                if (owner is None or s.owner == owner) and not s.hidden and (s.pinned or s.status in ("running", "queued")
                                     or (s.awaiting_kind == "decide" and now - s.updated < STALE)
                                     or now - s.updated < (CHAT_STALE if s.worked is False else STALE))]




@dataclass
class Job:
    schedule_id: str = field(default="", kw_only=True)
    schedule_run_id: str = field(default="", kw_only=True)
    text: str
    images: list[Path] = field(default_factory=list)
    finished: bool = False
    via: str = ""  # 从哪说的（web / dingtalk / auto，或扩展的名字）
    parts: list[str] = field(default_factory=list)  # 还没开工时你连着说的几句并成了这一件（网页上照旧一句一句显示）


Notify = Callable[[Session, Turn], Awaitable[None]]
Started = Callable[[Session, Turn, Job], Awaitable[None]]


class Runner:
    def __init__(self, registry: Registry, on_done: Notify, max_parallel: int = 3, default_effort: str | None = None,
                 on_start: Started | None = None, on_confirm: Notify | None = None, cfg: Config | None = None):
        self.registry = registry
        # 配置只有一份：回复语言、固定要求、子代理模型、DeepSeek 禁区、审核员这些都现读它（Bridge 换配置时跟着换）
        self.cfg = cfg or Config(default_effort=default_effort)
        self._cc_effort = user_setting("effortLevel") or ""   # Claude Code 自己设置里的默认深度
        self.on_confirm = on_confirm  # 有危险操作等你点头时（发通知、按钮卡）
        self.on_done = on_done
        self.on_start = on_start  # 开始干活时（发进度卡用）
        self.sem = asyncio.Semaphore(max_parallel)
        self.spare = asyncio.Semaphore(1)   # 预留给你接着说话的那一个名额（新开的活用不了）
        self.use_spare = False   # 总管打开时才用预留名额
        self.queues: dict[int, asyncio.Queue[Job]] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.live: dict[int, Any] = {}  # 正在跑的这一轮的连接（Claude 客户端 / Codex / ACP 连接；插话、停下用），引擎放、引擎拿
        self.tasks: set[asyncio.Task] = set()   # 后台小任务（插话、通知、起名）留着引用，免得被回收
        self.image_delegate = ImageDelegator(registry.path.parent / 'image_jobs') if registry else None
        self.image_tasks: dict[int, set[asyncio.Task]] = {}
        self.limits_gpt: dict[str, tuple[float, str | None]] = {}  # ChatGPT 会员的 Codex 额度（同上格式）
        self._gpt_usage_at = 0.0
        self.last: dict[int, Turn] = {}  # 每个对话刚做完的那一轮（记录写好前，网页先用它显示）
        self.turns: dict[int, Turn] = {}
        self.limits: dict[str, tuple[float, str | None]] = {}  # 额度：类型 → (已用比例, 何时重置)
        self._usage_at = 0.0
        self.on_usage: Callable[[Session, Turn], None] | None = None  # 每轮做完记一笔消耗（usage.py）
        self.role = None   # 特殊角色（总管）的额外工具和约定：role(session) → {"tools", "append", "disallowed", "own_slot", "on_stop"} 或 None
        self.waiting: set[int] = set()   # 已经从队列里取出、在等并行名额的对话（也算在忙）
        self.dropped: set[int] = set()   # 等名额时被叫停的：拿到名额也不做了
        self.finishing: set[int] = set()   # 这一轮刚做完、结果还在处理的（也算在忙）
        self.auto_title = False   # 头一轮做完自动起短名字（app 里打开；测试里不调模型）

    @property
    def default_effort(self) -> str:
        return self.cfg.default_effort or self._cc_effort

    @property
    def language(self) -> str:
        return self.cfg.reply_language

    @property
    def extra_instructions(self) -> str:
        return self.cfg.extra_instructions

    @property
    def subagent_model(self) -> str:
        """子代理默认用的模型全名；空 = 跟 Claude Code 默认。"""
        return engines.MODELS.get(self.cfg.subagent_model, "")

    @property
    def deepseek_block(self) -> list[str]:
        return self.cfg.deepseek_block

    @property
    def auto_review(self) -> bool:
        """危险命令交给审核员（reviewer.py）判断；关着就等你点。"""
        return self.cfg.auto_review

    @auto_review.setter
    def auto_review(self, on: bool) -> None:
        self.cfg.auto_review = on

    @property
    def review_policy(self) -> str:
        return self.cfg.review_policy

    def _bg(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @contextlib.asynccontextmanager
    async def _slot(self, own: bool, human: bool):
        """拿一个并行名额。own：自己有名额（总管）；human：你接着跟已有对话说话，名额满了可以用预留的那一个。"""
        if own:
            yield
        elif human and self.use_spare and self.sem.locked() and not self.spare.locked():
            async with self.spare:
                yield
        else:
            async with self.sem:
                yield

    def busy(self, num: int) -> bool:
        return num in self.turns or num in self.waiting or num in self.finishing or not self.queues.get(num, asyncio.Queue()).empty()

    def submit(self, s: Session, job: Job) -> int:
        """这个对话正在做：直接插话给它（返回 -1），它手上这一步做完就看到；否则排队，返回前面还排着几件。"""
        turn = self.turns.get(s.num)
        # 正在压缩的那一轮不插话（插进去可能被压缩吞掉），排到压完之后；能不能插、怎么插由引擎定
        if (turn and turn.accepting and not turn.interrupted and job.text != COMPACT
                and turn.request != _request_title(Job(text=COMPACT))
                and (steer := engines.get(s.engine).steer(self, s, turn, job)) is not None):
            turn.injected += 1
            turn.timeline.append({"t": time.time(), "kind": "you", "text": job.text})
            self._bg(steer)
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
        self._ensure_worker(s.num)
        return ahead

    def requeue(self, num: int, job: Job) -> None:
        """插话没插进去：排到下一轮。"""
        self.queues.setdefault(num, asyncio.Queue()).put_nowait(job)
        self._ensure_worker(num)

    def _ensure_worker(self, num: int) -> None:
        if num not in self.workers or self.workers[num].done():
            self.workers[num] = asyncio.create_task(self._worker(num))

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
        """危险操作停下来等你点头（手机通知 + 钉钉按钮 + 网页卡片）；20 分钟不回当不允许。各引擎共用（经 engines/policy.py）。"""
        fut = asyncio.get_running_loop().create_future()
        turn.confirm = {"what": what, "cmd": cmd[:600], "fut": fut}
        turn.timeline.append({"t": time.time(), "kind": "do", "text": f"等你确认：{what}", "detail": cmd[:400]})
        if self.on_confirm:
            self._bg(self.on_confirm(s, turn))
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


    async def stop(self, num: int) -> bool:
        q = self.queues.get(num)
        while q and not q.empty():
            q.get_nowait()
        if num in self.waiting:   # 还在等名额：拿到也不做了
            self.dropped.add(num)
            return True
        if num in self.turns:
            self.turns[num].interrupted = True
        image_tasks = list(self.image_tasks.get(num, ()))
        for task in image_tasks:
            task.cancel()
        if image_tasks:
            await asyncio.gather(*image_tasks, return_exceptions=True)
        if (conn := self.live.get(num)) is not None:   # 让正在跑的这一轮停下（各家停法不同）
            s = self.registry.get(num) if self.registry else None
            await engines.get(s.engine if s else None).halt(conn)
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
            # 总管有自己的名额：派出去的对话占满并行数时，它照样能听你说话、收回报
            own = bool(((self.role(s) if self.role else None) or {}).get("own_slot"))
            # 你自己接着跟已有对话说的话：名额满了也能用预留的那一个，新开的活挡不住你
            human = job.via in ("web", "dingtalk") and bool(s.claude_id or s.codex_id)
            async with contextlib.AsyncExitStack() as slot:
                self.waiting.add(num)
                try:   # 等名额时被取消（关机、重启）也要把“在等”清掉，不然这个对话一直算在忙
                    await slot.enter_async_context(self._slot(own, human))
                except BaseException:
                    self.dropped.discard(num)
                    raise
                finally:
                    self.waiting.discard(num)
                if num in self.dropped:
                    self.dropped.discard(num)
                    s.status = "idle"
                    self.registry.save()
                    continue
                turn = Turn(started=time.time(), request=_request_title(job), said=job.text[:SAID_MAX], via=job.via, last_at=time.time(), schedule_id=job.schedule_id, schedule_run_id=job.schedule_run_id)
                turn.sa_text = job.text   # 这一轮收到的整句话（总管的对话退回时要原样交回去）
                self.turns[num] = turn
                s.status = "running"
                self.registry.save()
                if self.on_start:
                    self._bg(self.on_start(s, turn, job))
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
                    self.finishing.add(num)   # 结果还没处理完（查额度、通知）：仍算在忙
                    self.turns.pop(num, None)
                    self.live.pop(num, None)
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
            try:
                if (eng := engines.get(s.engine)).has_quota:   # ACP 引擎没有额度可查
                    await eng.refresh_quota(self)
                if self.on_usage and turn.usage:
                    try:
                        self.on_usage(s, turn)
                    except Exception:  # noqa: BLE001 - 记不上消耗不影响报告
                        log.exception("记消耗失败")
                job.finished = True
                if self.auto_title and not (s.renamed or s.titled or turn.is_error or job.text == COMPACT):
                    self._bg(self._auto_title(s, job.text, turn.final_text))
                await self.on_done(s, turn)
            finally:
                self.finishing.discard(num)

    async def _run(self, s: Session, job: Job, turn: Turn) -> None:
        """跑一轮：交给这个对话的引擎（engines/）。测试里常替换掉它当假 AI。"""
        eng = engines.get(s.engine)
        if s.root and not eng.fenced_ok:
            raise RuntimeError("受限账号只能用 Claude")
        await eng.run(self, s, job, turn)


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
            if method in COMMAND_APPROVALS:   # 和对话里的 Codex 同一套判定（engines/policy.py）；原任务停了一律不批
                return await approve_command(self, s, turn, method, params, stopped=turn.interrupted)
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

