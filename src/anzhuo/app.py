"""远窗本体：收到你的话（网页或钉钉）→ 找到对话 → 交给 Claude / Codex → 把结果推回网页、手机通知和钉钉。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import list_sessions

from .localspend import LocalSpend
from . import cards, folders, report, translate, usage
from .commands import HELP, Cmd, parse
from .config import DATA_DIR, Config, write_web_hash, web_accounts
from .dingtalk import DingTalk, Incoming, NoDingTalk, parse_incoming
from .report import Turn
from .web import Web, relay_hint
from .sessions import COMPACT, Job, Registry, Runner, Session, describe, user_setting

MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
EFFORTS = {"低": "low", "中": "medium", "高": "high", "很高": "xhigh", "最高": "max", "默认": None}

# 接力：让长对话写一份交接说明，换到新对话接着做（新对话不用背着几十万 token 的旧记录）
RELAY = """这个对话太长了，要换到一个新对话接着做。请写一份交接说明，新对话只靠它接手，所以要具体：
1. 目标：用户要做成什么、明确提过的要求和偏好；
2. 已定下的决定和理由（含否掉的做法，免得重走）；
3. 已经做完的：改了哪些文件、提交 / 推送 / 上线情况；
4. 正在做的和接下来几步；
5. 关键路径、命令、账号位置、踩过的坑；
6. 还等用户决定的问题。
只回复交接说明本身，用 Markdown，2000 字以内；不要调用任何工具，不要再做别的事。"""

DINGTALK_FILE_MAX = 20 * 1024 * 1024  # 钉钉单个文件上限
RECENT_KEEP = 100  # recent.jsonl 只留最近这么多行（你发的、远窗发的），排查用

log = logging.getLogger(__name__)


_REPLY = re.compile(r"(好的?|好吧|行|可以|对|是的?|嗯+|ok|OK|继续|接着|就这样|选?[A-Da-d1-9]|再.{0,6}一点|为什么|然后呢|还有呢)[。！!？?…，,]*")
_CHOICE = re.compile(r"(就|那就)?(按|选|用|要)?\s*(方案)?\s*[A-Da-d]\s*(方案)?\s*(来做|来|吧|就行|好了|可以)?[。！!…，,]*")
_OPTIONS = re.compile(r"^\s*(?:\|\s*)?(?:[-*]\s*)?(?:\*\*)?(?:方案|选项)?\s*[A-D]\s*(?:\*\*)?\s*[.．、:：)）]", re.M)


def asks_user(text: str) -> str:
    """这一轮最后是不是在等你回答（要你定、列了 A/B/C、或以问句收尾）；是就返回那句问话，给分派用。"""
    body = text.strip()
    if not body:
        return ""
    last = [ln.strip(" *#") for ln in body.splitlines() if ln.strip()][-1]
    if report.needs_decision(body) or len(_OPTIONS.findall(body)) >= 2 or last.endswith(("？", "?")):
        return report.brief(last if last.endswith(("？", "?")) else body, 40)
    return ""


_OFFER = re.compile(r"(要不要|需不需要|是否需要|要我|需要我|我可以|我也可以|可以帮你|我再|顺便|还要我|想让我|如果你愿意|如果需要)")
_APPROVE = "批准本会话编辑："


def awaiting_kind(text: str) -> str:
    """等你的轻重：decide = 真要你拍板（要你定、列了方案、要你批准编辑、直接问你）；
    suggest = 活做完了，最后顺口问一句“要不要我再…？”——不算卡住，列表里不标琥珀色。"""
    body = text.strip()
    if not body:
        return ""
    if report.needs_decision(body) or len(_OPTIONS.findall(body)) >= 2 or _APPROVE in body:
        return "decide"
    lines = [ln.strip(" *#") for ln in body.splitlines() if ln.strip()]
    if not lines[-1].endswith(("？", "?")):
        return ""
    return "suggest" if _OFFER.search(lines[-1]) else "decide"


def limit_reset(text: str, now: float | None = None) -> float | None:
    """“You've hit your session limit · resets 5:40pm (Asia/Taipei)” → 几点恢复（本机时间戳）；不是撞额度返回 None。"""
    if not re.search(r"hit your .{0,20}limit|usage limit|limit reached", text or "", re.I):
        return None
    m = re.search(r"resets\s+(?:(\w{3})\w*\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    now = now or time.time()
    if not m:
        return now + 5 * 3600
    mon, day, hh, mm, ap = m.groups()
    h = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    base = datetime.fromtimestamp(now)
    months = {x: i for i, x in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
    if mon and mon.lower()[:3] in months:
        at = base.replace(month=months[mon.lower()[:3]], day=int(day), hour=h, minute=int(mm or 0), second=0, microsecond=0)
        if at.timestamp() < now - 86400:
            at = at.replace(year=at.year + 1)
        return at.timestamp()
    at = base.replace(hour=h, minute=int(mm or 0), second=0, microsecond=0)
    return at.timestamp() + (86400 if at.timestamp() <= now else 0)


def inline_overrides(text: str) -> tuple[str | None, str | None]:
    """话里明说了模型或深度（“用 opus”“深度调到高”），就以你说的为准。"""
    model = effort = None
    if m := re.search(r"(?:用|换成|改用|切到|模型)\s*(opus|sonnet|haiku|fable)", text, re.I):
        model = MODELS[m.group(1).lower()]
    elif re.search(r"(?:用|交给|让)\s*(codex|gpt|chatgpt)", text, re.I):
        model = "gpt"
    if m := re.search(r"(?:深度|思考)\s*(?:调到|调成|改成|设为|用|开)?\s*(很高|最高|低|中|高)", text):
        effort = EFFORTS[m.group(1)]
    return model, effort


def _looks_like_project(cwd: Path) -> bool:
    """手动新开时按文件夹猜：在代码仓库或项目里（有 .git、pyproject.toml、package.json）算开发。"""
    for d in [cwd, *list(cwd.parents)[:3]]:
        if any((d / m).exists() for m in (".git", "pyproject.toml", "package.json")):
            return True
    return False


def _is_reply(text: str) -> bool:
    """一看就是对上一条的回复（“好的”“选 B”“继续”），直接交给正在等你回答的对话。"""
    t = text.strip()
    return bool(_REPLY.fullmatch(t) or _CHOICE.fullmatch(t)) or len(t) <= 3


class Bridge:
    def _usage(self, s: Session, turn: Turn) -> None:
        last = None
        if s.engine != "codex" and not s.claude_usage:   # 对话里还没存累计值：用记录里这个对话最后一条的
            last = next((r.get("cum") for r in reversed(usage.load(self.data_dir / "usage.jsonl"))
                         if r.get("num") == s.num and r.get("cum")), None)
        if rec := usage.row(s, turn, self.runner.limits, last=last):
            usage.append(self.data_dir / "usage.jsonl", rec)
            self.registry.save()


    def __init__(self, cfg: Config, dt: DingTalk | NoDingTalk, data_dir: Path = DATA_DIR):
        self.cfg = cfg
        self.dt = dt
        self.data_dir = data_dir
        from .schedules import Schedules
        self.schedules = Schedules(data_dir / "schedules.json")
        self.registry = Registry(data_dir / "sessions.json")
        self.runner = Runner(self.registry, self._turn_done, cfg.max_parallel, cfg.default_effort, self._turn_started,
                             self._confirm_asked)
        self.runner.auto_review = cfg.auto_review
        self.runner.auto_title = True
        self.runner.language = cfg.reply_language
        self.runner.extra_instructions = cfg.extra_instructions
        translate.enabled = cfg.translate_english
        from . import schedules as _sch
        _sch.set_timezone(cfg.timezone)
        self.runner.review_policy = cfg.review_policy
        self.runner.on_usage = self._usage
        self.spend = LocalSpend(data_dir / "localspend.json")
        usage.migrate(data_dir / "usage.jsonl")
        self._relaying: set[int] = set()
        self.outgoing: dict[int, list[dict]] = {}  # 你刚发给各对话、Claude 记录里还没有的话（网页对话框先显示出来）
        self._compact_from: dict[int, float | None] = {}  # 压缩前的上下文用量，压完报“59% → 12%”
        self.board: str | None = None  # 工作台卡：所有在做的对话一张卡，始终挪到聊天最下面
        self.board_task: asyncio.Task | None = None
        self.board_lock = asyncio.Lock()
        self.board_shown = ""
        self.progress_every = (15.0, 45.0)  # 工作台多久更新一次：都在前 5 分钟 / 有做得久的（省钉钉调用次数）
        self.web = Web(cfg.web_base, cfg.web_port, data_dir, watch_host=cfg.watch_host,
                       accounts=web_accounts(cfg) if cfg.web_base else {}, save_hash=write_web_hash,
                       web_prefix=cfg.web_prefix)
        self.web.bridge = self
        self.cards_path = data_dir / "cards.json"
        self.cards: dict[str, dict] = self._load_cards()  # 卡片 → 它属于哪个对话、是什么卡；重启后按钮还能用
        self.pending: dict | None = None  # 等你回编号选文件夹 / 选最近的对话
        self.photo_buf: Incoming | None = None  # 只发了图、还没跟话时先攒着，等后面那句话一起交
        self.photo_task: asyncio.Task | None = None
        self.photo_wait = 15.0
        self.receipt_delay = 4.0
        self.user_id = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else "local"
        for s in self.registry.sessions.values():
            if s.status in ("running", "queued"):
                s.status = "error"
        self.registry.save()

    def create_schedule(self, data: dict) -> dict:
        target = self.registry.get(int(data["target"])) if data.get("target") else None
        if data.get("target") and (not target or target.hidden):
            raise ValueError("目标对话不存在")
        model = data.get("model") or "auto"
        if target and model != "auto" and (model == "gpt") != (target.engine == "codex"):
            raise ValueError("已有对话不能切换 Claude / Codex，请选择继承模型或新开专用对话")
        if data.get("folder") and data["folder"] not in self.cfg.folders:
            raise ValueError("请选择已配置的工作文件夹")
        return self.schedules.upsert(data)

    async def _schedule_failed(self, task: dict):
        body = f"{task['title']}：{task['error']}。计划已暂停，可在定时任务中修改。"
        pushed = await self.web.notify("admin", "定时任务未执行", body, task.get("target")) if self.web.enabled else 0
        if not pushed:
            await self.say("定时任务未执行", body, board=False)

    def _scheduled_busy(self, num: int) -> bool:
        s = self.registry.get(num)
        return self.runner.busy(num) or bool(s and s.status in ("queued", "running"))

    async def _dispatch_schedule(self, task: dict) -> int:
        s = self.registry.get(task.get("target"))
        if task.get("target") and (not s or s.hidden):
            raise ValueError("目标对话已收起或不存在，请修改任务")
        if s and self._scheduled_busy(s.num):
            raise ValueError("目标对话正忙，请稍后重新安排")
        model = task["model"]
        if s and model != "auto" and (model == "gpt") != (s.engine == "codex"):
            raise ValueError("目标对话的模型类型发生变化，请修改任务")
        if not s:
            num = self.registry.reserve_num()
            folder = task.get("folder")
            cwd = Path(self.cfg.folders[folder]).expanduser() if folder else folders.scratch_dir(self.cfg, num)
            if not cwd.is_dir(): raise ValueError("工作文件夹不存在")
            if why := await asyncio.to_thread(folders.blocked, [cwd]):
                raise ValueError("工作文件夹不可访问")
            pick = model if model != "auto" else (self.cfg.dev_model if _looks_like_project(cwd) else self.cfg.simple_model)
            s = Session(num=num, name=task['title'], cwd=str(cwd), engine="codex" if pick == "gpt" else "claude",
                        model=None if pick == "gpt" else MODELS[pick], effort=task['effort'], renamed=True, titled=True)
            current = self.registry.current
            self.registry.add(s)
            self.registry.current = current
            self.registry.save()
            task['target'] = s.num
            self.schedules.save()
        else:
            if model != "auto" and model != "gpt": s.model = MODELS[model]
            s.effort = task['effort']
        s.awaiting = s.awaiting_kind = ""
        self.registry.save()
        job = Job(text=task['prompt'], via="schedule", schedule_id=task['id'], schedule_run_id=task['run_id'])
        self.outgoing.setdefault(s.num, []).append({'t': time.time(), 'text': task['prompt']})
        self.runner.submit(s, job)
        return s.num

    # ---------- 发消息 ----------
    def record(self, who: str, text: str, **extra) -> None:
        """最近的来往原文存一份在本机（你同意的），排查“远窗为什么这么答”时看原话。只留最近 100 行。"""
        try:
            path = self.data_dir / "recent.jsonl"
            lines = path.read_text().splitlines()[-(RECENT_KEEP - 1):] if path.exists() else []
            row = {"t": time.strftime("%m-%d %H:%M:%S"), "who": who, "text": text[:2000], **extra}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join([*lines, json.dumps(row, ensure_ascii=False)]) + "\n")
        except OSError:
            log.debug("记最近来往失败", exc_info=True)

    async def detail(self, title: str, text: str) -> None:
        """过程类消息（开工、收到、插话、开始压缩）：钉钉安静模式下不发，只记下来（网页里本来就看得到）。"""
        if self.cfg.dingtalk_quiet:
            self.record("远窗（只记不发）", text, title=title)
            return
        await self.say(title, text)

    async def say(self, title: str, text: str, board: bool = True) -> None:
        self.record("远窗", text, title=title)
        for i, part in enumerate(report.split(text)):
            await self.dt.send_markdown(self.user_id, title if i == 0 else f"{title}（续）", part)
        if board:
            await self._board_follow()

    def _load_cards(self) -> dict[str, dict]:
        try:
            return json.loads(self.cards_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cards(self) -> None:
        keep = dict(list(self.cards.items())[-100:])
        self.cards = keep
        self.cards_path.parent.mkdir(parents=True, exist_ok=True)
        self.cards_path.write_text(json.dumps(keep, ensure_ascii=False))

    def _active(self) -> list[tuple[str, int, Turn | None]]:
        """在做的和排队的对话，给工作台用。"""
        items = []
        for num, s in sorted(self.registry.sessions.items()):
            if s.owner != "admin":
                continue
            if num in self.runner.turns:
                items.append((s.label, num, self.runner.turns[num]))
            elif self.runner.busy(num):
                items.append((s.label, num, None))
        return items

    async def _board_post(self) -> None:
        """在聊天最下面发一张新的工作台，旧的那张缩成一行“挪到下面了”。"""
        async with self.board_lock:
            old, oid = self.board, f"b-{int(time.time() * 1000)}"
            data = cards.board_data(self._active())
            await self.dt.create_card(self.user_id, oid, data)
            self.board, self.board_shown = oid, data["markdown"]
            self.cards[oid] = {"kind": "board", "num": 0}
            self._save_cards()
            if old:
                try:
                    await self.dt.update_card(old, cards.board_data([], moved=True))
                except Exception:  # noqa: BLE001
                    log.debug("旧工作台收起失败", exc_info=True)
        if not self.board_task or self.board_task.done():
            self.board_task = asyncio.create_task(self._board_loop())

    async def _board_follow(self) -> None:
        """远窗刚发了别的消息：还有活在做，就把工作台挪到它下面，免得被顶上去看不到；都做完了就当场收尾。"""
        if not self.board:
            return
        try:
            if self._active():
                await self._board_post()
            else:
                async with self.board_lock:
                    oid, self.board = self.board, None
                    if oid:
                        await self.dt.update_card(oid, cards.board_data([]))
        except Exception:  # noqa: BLE001
            log.debug("挪工作台失败", exc_info=True)

    async def _board_loop(self) -> None:
        """原地更新工作台；都做完了就改成“都做完了”，不再跟着挪。"""
        while self.board:
            items = self._active()
            long = any(t and time.time() - t.started > 300 for _, _, t in items)
            if not items:
                async with self.board_lock:
                    oid, self.board = self.board, None
                    if oid:
                        try:
                            await self.dt.update_card(oid, cards.board_data([]))
                        except Exception:  # noqa: BLE001
                            log.debug("工作台收尾失败", exc_info=True)
                return
            data = cards.board_data(items)
            if data["markdown"] != self.board_shown and self.board:
                try:
                    await self.dt.update_card(self.board, data)
                    self.board_shown = data["markdown"]
                except Exception:  # noqa: BLE001
                    log.debug("更新工作台失败", exc_info=True)
            await asyncio.sleep(self.progress_every[long])

    async def _turn_started(self, s: Session, turn: Turn, job: Job) -> None:
        """几秒内答完的不打扰；做得久的上工作台（没有就发一张）。网页开着时进度只在网页里看，钉钉只在钉钉里说的话才发。"""
        if job.schedule_id:
            self.schedules.started(job.schedule_id, job.schedule_run_id)
        await asyncio.sleep(self.receipt_delay)
        if job.finished or turn.ended or self.cfg.dingtalk_quiet or (self.web.enabled and job.via != "dingtalk"):
            return
        if self.board:
            return  # 工作台自己会刷新出这一件
        try:
            await self._board_post()
        except Exception:  # noqa: BLE001 - 卡片发不出就退回一句文字
            log.warning("发工作台失败", exc_info=True)
            await self.say("收到", f"收到 · {s.label} 在做：{turn.request or '看图'}", board=False)

    async def compact(self, num: int | None, via: str = "dingtalk") -> str:
        """压缩某个对话的上下文：发 /compact 给它；正在做就排到这一轮之后。返回给网页的一句话。"""
        s = self.registry.get(num or self.registry.current)
        if not s or not (s.claude_id or s.codex_id):
            msg = "这个对话还没开始，没什么可压缩的。"
            await self.say("压缩", msg)
            return msg
        if state := self.runner.compacting(s.num):   # 点了两次也只压一次
            return f"{s.label} " + ("正在压缩，大约一分钟。" if state == "running" else "已经排着要压缩了，手上的做完就压。")
        self._compact_from[s.num] = s.context_pct
        ahead = self.runner.submit(s, Job(text=COMPACT, via=via))
        msg = f"{s.label} 开始压缩上下文" + (f"，等手上 {ahead} 件做完再压" if ahead else "") + "，大约一分钟。"
        if via == "dingtalk" or not self.web.enabled:
            await self.detail("压缩", msg)
        return msg

    def relay(self, num: int, via: str = "web") -> str:
        """接力：让这个对话写交接说明，写完开一个新对话（同一个文件夹、模型、深度），交接说明随你第一句话交给它。"""
        s = self.registry.get(num)
        if not s or not (s.claude_id or s.codex_id):
            return "这个对话还没开始，不用接力。"
        if s.relay_to and self.registry.get(s.relay_to):
            return f"已经接力到 #{s.relay_to} 了。"
        if num in self._relaying:
            return "正在写交接说明，写完自动开新对话。"
        if self.runner.busy(num):
            return "正在回答或还有排队消息，请等回答结束后再接力。"
        if not s.relay_suggested and not relay_hint(s, False, False):
            return "这个对话暂时没有接力建议。"
        self._relaying.add(num)
        ahead = self.runner.submit(s, Job(text=RELAY, via=via))
        return "正在写交接说明" + (f"，等手上 {ahead} 件做完再写" if ahead else "") + "，写完自动开新对话。"

    def _relay_open(self, p: Session, note: str) -> Session:
        now = time.time()
        s = Session(num=self.registry.reserve_num(), name=p.name, cwd=p.cwd, add_dirs=list(p.add_dirs),
                    owner=p.owner, root=p.root, engine=p.engine, model=p.model, effort=p.effort, pinned=p.pinned,
                    task_group=p.task_group, task_group_manual=p.task_group_manual,
                    renamed=True, titled=True, worked=p.worked, seen=now, updated=now, relay_from=p.num, relay_note=note,
                    unsent=f"（这是接力的新对话：之前在另一个对话里做，那边太长了换到这里。下面是那边写的交接说明，按它接着做；"
                           f"不清楚的细节先看现场文件，或问用户。）\n\n{note}\n\n---\n用户这次说：")
        self.registry.add(s)
        p.relay_to = s.num
        self.registry.save()
        return s

    async def _turn_done(self, s: Session, turn: Turn) -> None:
        if s.num in self._relaying:
            self._relaying.discard(s.num)
            note = (turn.final_text or "").strip()
            if not (turn.is_error or turn.interrupted) and note:
                self._relay_open(s, note)
        if turn.schedule_id:
            self.schedules.finish(turn.schedule_id, turn.schedule_run_id, turn.error or ("任务未完成" if turn.is_error or turn.interrupted else ""))
        if turn.request == "压缩上下文" and not turn.is_error:
            before = self._compact_from.pop(s.num, None)
            after = s.context_pct
            span = f"{before:.0f}% → {after:.0f}%" if before is not None and after is not None else (f"现在 {after:.0f}%" if after is not None else "")
            if self.web.enabled and turn.via != "dingtalk":
                return   # 网页里点的压缩，网页上看得到结果
            return await self.say("压缩好了", f"{s.label} 压缩好了：上下文 {span}。之前聊过的要点都还记得。")
        if translate.needs_translation(turn.final_text):
            if done := await translate.to_chinese(turn.final_text):
                turn.final_text, turn.translated = done
        title, body = report.build(s.label, turn, s.cwd, self.runner.limits_gpt if s.engine == "codex" else self.runner.limits)
        if self.web.enabled:
            try:
                tid = self.web.save_turn(s.label, turn, s.cwd, title.split(" · ")[0] if title else "回复")
                if not (turn.changed_files or turn.commands or turn.images or turn.is_error):
                    raise LookupError  # 闲聊、直接问答：记下来给网页对话框用，报告里不附链接
                turn.links = [("完整过程", self.web.link(f"/_x/t/{tid}"))]
                title, body = report.build(s.label, turn, s.cwd, self.runner.limits_gpt if s.engine == "codex" else self.runner.limits)
            except LookupError:
                pass
            except Exception:  # noqa: BLE001 - 过程页出问题不影响报告
                log.exception("记过程页失败")
        s.last_title = title
        s.last_summary = f"你说“{turn.request}”，它答“{report.brief(turn.final_text, 60)}”"
        s.awaiting = "" if turn.is_error else asks_user(turn.final_text)
        s.awaiting_kind = awaiting_kind(turn.final_text) if s.awaiting else ""
        if turn.is_error and (until := limit_reset(turn.error or turn.final_text)):
            s.limit_until = until   # 撞上 5 小时额度：恢复后一分钟自动接着做
            asyncio.create_task(self._auto_resume(s.num, until))
        self.registry.save()
        # 通知：你正开着这个对话看就都不发；不然先弹手机通知，弹不出去才用钉钉说一句（带链接）。
        # 钉钉里说的话、或没开网页时，照旧把完整结果发回钉钉。
        viewing = self.web.enabled and self.web.watching(s.num)
        full = (turn.via == "dingtalk" or not self.web.enabled) and s.owner == "admin"
        pushed = 0
        if self.web.enabled and not viewing:
            head, what = report.push_message(s.name, turn.request, turn.final_text, turn.is_error, s.awaiting,
                                             s.awaiting_kind, s.limit_until if turn.is_error else 0)
            pushed = await self.web.notify(s.owner, head, what, s.num)
        if not full:
            if not viewing and not pushed and s.owner == "admin":
                head, what = report.push_message(s.name, turn.request, turn.final_text, turn.is_error, s.awaiting,
                                                 s.awaiting_kind, s.limit_until if turn.is_error else 0)
                try:
                    await self.say(f"#{s.num} {what[:20]}", f"**#{s.num} {s.name}**　{what}　[在远窗看]({self.web.link(f'/_x/#s{s.num}')})",
                                   board=False)
                except Exception:  # noqa: BLE001
                    log.exception("钉钉提醒 #%s 失败", s.num)
            return
        try:
            await self.say(title, body, board=False)
            for img in turn.images:
                await self.dt.send_image(self.user_id, Path(img))
            for f in turn.files:
                path = Path(f)
                if path.stat().st_size > DINGTALK_FILE_MAX:
                    await self.say("文件太大", f"{path.name} 超过 20MB，钉钉发不了，到网页对话里下载。", board=False)
                    continue
                try:
                    await self.dt.send_file(self.user_id, path)
                except Exception:  # noqa: BLE001
                    log.exception("发文件失败")
                    await self.say("文件没发出去", f"{path.name} 钉钉没发出去，到网页对话里下载。", board=False)
            if not turn.is_error and (d := cards.parse_decision(turn.final_text)):
                oid = f"d{s.num}-{int(time.time() * 1000)}"
                await self.dt.create_card(self.user_id, oid, cards.decision_data(s.label, d))
                self.cards[oid] = {"kind": "decision", "num": s.num, "question": d.question,
                                   "options": d.options, "recommend": d.recommend}
                self._save_cards()
        except Exception:
            log.exception("推送 #%s 的结果失败", s.num)
        await self._board_follow()

    async def _auto_resume(self, num: int, until: float) -> None:
        """额度恢复后一分钟，替你说一句“继续”（你点了“别自动接”、或已经自己接着说了就不说）。"""
        await asyncio.sleep(max(0.0, until + 60 - time.time()))
        s = self.registry.get(num)
        if not s or s.limit_until != until or not s.auto_resume or self.runner.busy(num) or s.hidden:
            return
        s.limit_until = 0.0
        self.record("远窗", "额度恢复，自动接着做", num=num)
        inc = Incoming(sender_id=self.user_id, sender_nick="", conversation_type="1", kind="text", text="继续", via="auto")
        await self._submit(s, "继续", inc)

    async def _confirm_asked(self, s: Session, turn: Turn) -> None:
        """有危险操作等你点头：手机通知 + 钉钉按钮卡（网页对话里也会出现允许 / 不允许）。"""
        c = turn.confirm
        if not c:
            return
        self.record("确认", c["what"], num=s.num)
        if self.web.enabled:
            asyncio.create_task(self.web.notify(s.owner, s.name, "等你确认：" + c["what"], s.num))
        if s.owner != "admin":
            return
        try:
            oid = f"c{s.num}-{int(time.time() * 1000)}"
            await self.dt.create_card(self.user_id, oid, cards.confirm_data(s.label, c["what"], c["cmd"], s.num))
            self.cards[oid] = {"kind": "confirm", "num": s.num, "what": c["what"], "cmd": c["cmd"]}
            self._save_cards()
        except Exception:  # noqa: BLE001 - 钉钉发不出去，网页和手机通知照样能点
            log.exception("发确认卡失败")

    async def answer_confirm(self, num: int, allow: bool, where: str) -> bool:
        """不管在哪点的（网页 / 钉钉），都走这里：交给对话，把钉钉卡改成结果。"""
        if not self.runner.answer_confirm(num, allow):
            return False
        result = ("你允许了" if allow else "你没允许") + f"（{where}）"
        self.record("你", f"确认 #{num}：{result}")
        for oid, info in self.cards.items():
            if info.get("kind") == "confirm" and info["num"] == num and not info.get("done"):
                info["done"] = True
                s = self.registry.get(num)
                try:
                    await self.dt.update_card(oid, cards.confirm_data(s.label if s else f"#{num}", info["what"],
                                                                      info["cmd"], num, result))
                except Exception:  # noqa: BLE001
                    log.exception("改确认卡失败")
        self._save_cards()
        return True

    async def on_card(self, data: dict) -> None:
        """卡片上的按钮被点了：停下，或把选择交回那个对话。"""
        if data.get("userId") not in self.cfg.allowed_user_ids:
            log.warning("忽略一次卡片点击：%s", data.get("userId"))
            return
        oid, action = data.get("outTrackId", ""), cards.action_of(data)
        info = self.cards.get(oid)
        log.info("卡片点击：%s %s", oid, action)
        self.record("你", f"点了卡片按钮 {action}", card=oid, raw=json.dumps(data, ensure_ascii=False)[:1500])
        if not info:
            return await self.say("按钮失效了", "这张卡片的按钮已经失效，直接回字告诉我吧。")
        if action.startswith(("allow:", "deny:")):
            num = int(action.split(":")[1])
            if not await self.answer_confirm(num, action.startswith("allow"), "钉钉"):
                await self.say("确认", f"#{num} 这一步已经过去了（点过了或超时了）。")
            return
        if action.startswith("stop:"):
            num = int(action.split(":")[1])
            if not await self.runner.stop(num):
                await self.say("停", f"#{num} 已经做完了，不用停。")
            return
        if not action:
            return await self.say("按钮没认出来", "这个按钮我没认出来（已记下来排查），先直接回字告诉我选哪个吧。")
        s = self.registry.get(info["num"])
        if not s:
            return await self.say("没有这个对话", f"#{info['num']} 已经不在了。")
        if action == "stop":
            stopped = await self.runner.stop(s.num)
            if not stopped:
                await self.say("停", f"{s.label} 已经做完了，不用停。")
            return
        if action.startswith("pick:") and info["kind"] == "decision":
            if info.get("picked"):
                return
            letter = action.split(":", 1)[1]
            options = dict(info["options"])
            info["picked"] = letter
            self._save_cards()
            d = cards.Decision(info["question"], [tuple(o) for o in info["options"]], info.get("recommend"))
            await self.dt.update_card(oid, cards.decision_data(s.label, d, picked=letter))
            text = f"选 {letter}：{options.get(letter, '')}"
            inc = Incoming(sender_id=self.user_id, sender_nick="", conversation_type="1", kind="text", text=text)
            await self._message(Cmd("message", num=s.num, text=text), inc, routed=True)

    # ---------- 收消息 ----------
    async def on_raw(self, data: dict) -> None:
        inc = parse_incoming(data)
        if inc.sender_id not in self.cfg.allowed_user_ids or inc.conversation_type != "1":
            log.warning("忽略一条消息：发送人 %s（%s），会话类型 %s", inc.sender_id, inc.sender_nick, inc.conversation_type)
            return
        log.info("收到 %s，文字 %d 字，图片 %d，文件 %d，引用 %s", inc.kind, len(inc.text), len(inc.image_codes),
                 len(inc.files), "有" if inc.quoted_text else "无")
        self.record("你", inc.text, kind=inc.kind, images=len(inc.image_codes), files=len(inc.files),
                    quoted=inc.quoted_text[:200])
        try:
            await self.handle(inc)
        except Exception as e:  # noqa: BLE001
            log.exception("处理消息失败")
            await self.say("出错了", f"远窗处理这条消息时出错：{type(e).__name__}: {str(e)[:200]}")

    async def handle(self, inc: Incoming) -> None:
        if inc.kind == "audio" and not inc.text:
            return await self.say("没听清", "这条语音没拿到文字。可以用键盘上的麦克风听写，说完再发。")
        if inc.kind not in ("text", "picture", "richText", "audio", "file"):
            return await self.say("看不懂", f"这种消息（{inc.kind}）我还处理不了，请发文字、图片或文件。")
        if inc.image_codes and not inc.text:
            if self.photo_buf:
                self.photo_buf.image_codes += inc.image_codes
            else:
                self.photo_buf = inc
                self.photo_task = asyncio.create_task(self._flush_photos())
            return
        if self.photo_buf and inc.text:
            inc.image_codes = self.photo_buf.image_codes + inc.image_codes
            self.photo_buf = None
            if self.photo_task:
                self.photo_task.cancel()
        cmd = parse(inc.text) if inc.text else Cmd("message")
        if getattr(cmd, "num", None) and cmd.kind != "pick" and not self.registry.mine(cmd.num):
            cmd.num = -1   # 别的账号的对话：钉钉里当作没有
        if cmd.kind == "pick" and self.pending:
            return await self._pick(cmd.num)
        if cmd.kind == "pick":
            cmd = Cmd("message", text=inc.text)
        self.pending = None if cmd.kind != "message" else self.pending

        if cmd.kind == "help":
            return await self.say("用法", HELP)
        if cmd.kind == "list":
            return await self._list(show_all="全部" in cmd.text)
        if cmd.kind == "delete":
            return await self._delete(cmd.num)
        if cmd.kind == "switch":
            s = self.registry.get(cmd.num)
            if not s:
                return await self.say("没有这个对话", f"没有 #{cmd.num}。发“列表”看看有哪些。")
            self.registry.current = s.num
            self.registry.save()
            return await self.say("已切换", f"之后直接说的话交给 {s.label}。")
        if cmd.kind == "stop":
            num = cmd.num or self.registry.current
            s = self.registry.get(num)
            if not s:
                return await self.say("没有这个对话", "没找到要停的对话。")
            stopped = await self.runner.stop(s.num)
            return await self.say("停", f"{s.label}：{'已让它停下，稍后发停下时的情况' if stopped else '本来就没在做，排队的也清掉了'}。")
        if cmd.kind == "rename":
            s = self.registry.get(cmd.num)
            if not s:
                return await self.say("没有这个对话", f"没有 #{cmd.num}。")
            s.name = cmd.name[:30]
            self.registry.save()
            return await self.say("已改名", s.label)
        if cmd.kind in ("model", "effort"):
            return await self._set_model(cmd)
        if cmd.kind == "web":
            if not self.web.enabled:
                return await self.say("网页", "网页没有开启（配置里 web = false）。")
            return await self.say("网页", f"远窗网页：{self.web.base}\n\n用账号密码登录，一次登录 30 天免登录。"
                                          "里面有全部对话、每一轮的完整过程，以及换模型、定时任务这些设置。")
        if cmd.kind == "compact":
            return await self.compact(cmd.num)
        if cmd.kind == "quota":
            return await self._quota()
        if cmd.kind == "recent":
            return await self._recent(cmd.folders[0])
        if cmd.kind == "desktop":
            return await self._desktop(cmd.num)
        if cmd.kind == "resume":
            return await self._recent(cmd.folders[0], pick=cmd.num)
        if cmd.kind == "new":
            return await self._new(cmd.folders, cmd.text, inc)
        if cmd.kind == "mkdir":
            return await self._mkdir(cmd.folders[0] if cmd.folders else "", cmd.text, inc)
        await self._message(cmd, inc)

    async def _flush_photos(self) -> None:
        await asyncio.sleep(self.photo_wait)
        inc, self.photo_buf = self.photo_buf, None
        if inc:
            await self._message(Cmd("message"), inc)

    # ---------- 指令 ----------
    async def _list(self, show_all: bool = False) -> None:
        if not self.registry.sessions:
            return await self.say("对话列表", "还没有对话。发“新开 文件夹：要做的事”开一个，发“帮助”看用法。")
        visible = self.registry.visible()
        shown = list(self.registry.sessions.values()) if show_all else visible
        rows = []
        for s in sorted(shown, key=lambda x: -x.updated)[:30 if show_all else 15]:
            mark = "（当前）" if s.num == self.registry.current else ""
            mark += "（收起）" if s not in visible else ""
            rows.append(f"- {describe(s, s.num in self.runner.turns)}{mark}")
        hidden = len(self.registry.sessions) - len(visible)
        if not rows:
            rows.append("最近 7 天没有在动的对话。")
        if hidden and not show_all:
            rows.append(f"\n另有 {hidden} 个收起了（7 天没动或删掉的），发“列表 全部”看；发“#编号 …”可以接着聊。")
        await self.say("对话列表", "\n".join(rows))

    async def _delete(self, num: int | None) -> None:
        s = self.registry.get(num)
        if not s:
            return await self.say("没有这个对话", f"没有 #{num}。发“列表”看看有哪些。")
        if s.num in self.runner.turns or s.status in ("running", "queued"):
            return await self.say("还在做", f"{s.label} 还在做，先发“停 {s.num}”，停下后再删。")
        s.hidden, s.hidden_at, s.pinned = True, time.time(), False
        if self.registry.current == s.num:
            self.registry.current = None
        self.registry.save()
        await self.say("已收起", f"{s.label} 收起了：不再出现在列表里，远窗也不会再把话交给它。"
                                  f"聊天记录还在，要找回发“#{s.num} 接着说的话”。")

    async def _set_model(self, cmd: Cmd) -> None:
        s = self.registry.get(cmd.num or self.registry.current)
        if not s:
            return await self.say("没有这个对话", "先开一个对话，或写上编号，比如“模型 7 sonnet”。")
        if cmd.kind == "model":
            if not cmd.name:
                return await self.say("模型", f"{s.label} 用的是 {report.model_name(MODELS.get(s.model or '', s.model or '')) or '默认（' + str(user_setting('model')) + '）'}。")
            if cmd.name not in MODELS and cmd.name != "默认":
                return await self.say("模型", "可选：" + "、".join(MODELS) + "、默认")
            s.model = None if cmd.name == "默认" else MODELS[cmd.name]
            shown = report.model_name(s.model) if s.model else "默认"
        else:
            if not cmd.name:
                cur = s.effort or self.runner.default_effort
                return await self.say("深度", f"{s.label} 的思考深度：{report.EFFORT_CN.get(cur, cur) or '默认'}。")
            if cmd.name not in EFFORTS:
                return await self.say("深度", "可选：" + "、".join(EFFORTS))
            s.effort = EFFORTS[cmd.name]
            shown = cmd.name
        self.registry.save()
        await self.say("已设置", f"{s.label} 从下一句起用：{shown}。")

    async def _quota(self) -> None:
        await self.runner.refresh_usage(force=True)
        if not self.runner.limits:
            return await self.say("额度", "这次没查到额度，稍后再试。")
        lines = []
        for key, (util, resets) in self.runner.limits.items():
            when = f"，{resets} 重置" if resets else ""
            lines.append(f"- {report.LIMIT_CN.get(key, key)}：已用 {util * 100:.0f}%{when}")
        await self.say("额度", "\n".join(lines))

    async def _resolve_all(self, tokens: list[str]) -> tuple[list[Path], str | None, list[Path]]:
        """返回 (已确定的目录, 有歧义的词, 该词的候选)。"""
        done: list[Path] = []
        for t in tokens:
            c = folders.candidates(t, self.cfg)
            if not c:
                raise LookupError(t)
            if len(c) > 1:
                return done, t, c
            done.append(c[0])
        return done, None, []

    async def _new(self, tokens: list[str], text: str, inc: Incoming, resolved: list[Path] | None = None,
                   note: str = "", kind: str | None = None, model: str | None = None, effort: str | None = None) -> None:
        resolved = list(resolved or [])
        rest = tokens[len(resolved):]
        if not tokens:
            num = self.registry.reserve_num()
            cwd = folders.scratch_dir(self.cfg, num)
            return await self._start(num, cwd, [], text, inc, note=note or "（没指定文件夹，用了临时文件夹）", kind=kind,
                                     model=model, effort=effort)
        try:
            more, ambiguous, cands = await self._resolve_all(rest)
        except LookupError as e:
            return await self.say("没找到文件夹", f"没找到“{e.args[0]}”。可以写短名（{'、'.join(self.cfg.folders)}）、完整路径，或文件夹名里更具体的几个字。")
        resolved += more
        if ambiguous:
            self.pending = {"kind": "folder", "tokens": tokens, "resolved": resolved, "cands": cands,
                            "text": text, "inc": inc, "note": note, "task_kind": kind}
            lines = [f"{i}. {folders.display(p)}" for i, p in enumerate(cands, 1)]
            return await self.say("选文件夹", f"“{ambiguous}”找到几个，回编号选一个：\n\n" + "\n".join(lines))
        num = self.registry.reserve_num()
        await self._start(num, resolved[0], resolved[1:], text, inc, note=note, kind=kind, model=model, effort=effort)

    async def _mkdir(self, spec: str, text: str, inc: Incoming) -> None:
        parent_tok, name = folders.split_new_path(spec)
        if not name or "/" in name or name.startswith("."):
            return await self.say("新建文件夹", "要写成“新建 位置/名字：要做的事”，比如“新建 personal/菜谱整理：…”。")
        if parent_tok:
            cands = folders.candidates(parent_tok, self.cfg)
            if len(cands) != 1:
                return await self.say("新建文件夹", f"位置“{parent_tok}”{'没找到' if not cands else '有好几个，请写完整路径或短名'}。")
            parent = cands[0]
        else:
            parent = Path(self.cfg.scratch_root).expanduser()
        target = parent / name
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        num = self.registry.reserve_num()
        await self._start(num, target, [], text, inc, note="（文件夹本来就有，直接用了）" if existed else "（已新建文件夹）")

    async def _pick(self, n: int | None) -> None:
        p, self.pending = self.pending, None
        if p["kind"] == "folder":
            if not n or not 1 <= n <= len(p["cands"]):
                return await self.say("选文件夹", "编号不对，这次先取消了，重新发一次“新开 …”吧。")
            await self._new(p["tokens"], p["text"], p["inc"], resolved=p["resolved"] + [p["cands"][n - 1]],
                            note=p.get("note", ""), kind=p.get("task_kind"))
        elif p["kind"] == "desktop":
            await self._desktop(n, p["infos"], replace=p["num"])
        elif p["kind"] == "recent":
            await self._recent(p["folder"], pick=n)

    async def _recent(self, token: str, pick: int | None = None) -> None:
        cands = folders.candidates(token, self.cfg)
        if len(cands) != 1:
            return await self.say("最近的对话", f"文件夹“{token}”{'没找到' if not cands else '有好几个，请写短名或完整路径'}。")
        d = cands[0]
        infos = await asyncio.to_thread(list_sessions, str(d), 10, 0, False)
        if not infos:
            return await self.say("最近的对话", f"{folders.display(d)} 里还没有对话。")
        if pick is None:
            lines = []
            for i, x in enumerate(infos, 1):
                when = datetime.fromtimestamp(x.last_modified / 1000 if x.last_modified > 1e12 else x.last_modified)
                title = (x.custom_title or x.summary or x.first_prompt or "").replace("\n", " ")[:40]
                lines.append(f"{i}. {when:%m-%d %H:%M}　{title}")
            self.pending = {"kind": "recent", "folder": token}
            return await self.say("最近的对话", f"{folders.display(d)} 最近的对话，回编号接着聊：\n\n" + "\n".join(lines))
        if not 1 <= pick <= len(infos):
            return await self.say("最近的对话", "编号不对。")
        x = infos[pick - 1]
        num = self.registry.reserve_num()
        name = report.brief(x.custom_title or x.summary or x.first_prompt or "接着聊", 16)
        s = self.registry.add(Session(num=num, name=name, cwd=str(d), claude_id=x.session_id, from_desktop=True))
        await self.say("接上了", f"{s.label} 接上了，之后直接说的话交给它。")

    async def _desktop(self, pick: int | None, infos: list | None = None, replace: int | None = None) -> None:
        """接上你最近在桌面端聊的那段：不用记文件夹和编号。远窗自己新开的、临时文件夹里的不算。"""
        if infos is None:
            mine = {s.claude_id for s in self.registry.sessions.values() if not s.from_desktop}
            skip = (str(Path.home()), str(Path(self.cfg.scratch_root).expanduser()))
            everything = await asyncio.to_thread(list_sessions, None, 150, 0, False)
            infos = [x for x in everything
                     if x.session_id not in mine and x.cwd and x.cwd != skip[0] and not x.cwd.startswith(skip[1])][:5]
        if not infos:
            return await self.say("接着桌面", "没找到桌面端最近的对话。")
        pick = pick or 1
        if not 1 <= pick <= len(infos):
            return await self.say("接着桌面", f"编号不对，只有 1—{len(infos)}。")
        if replace and (old := self.registry.get(replace)) and old.status == "idle" and not old.hidden:
            old.hidden = True  # 刚才接错的那段收起来
        x = infos[pick - 1]
        title = (x.custom_title or x.summary or x.first_prompt or "接着聊").replace("\n", " ")
        s = next((v for v in self.registry.sessions.values() if v.claude_id == x.session_id), None)
        new = s is None
        if s:
            s.hidden = False
            self.registry.current = s.num
            self.registry.save()
        else:
            s = self.registry.add(Session(num=self.registry.reserve_num(), name=report.brief(title, 16),
                                          cwd=x.cwd, claude_id=x.session_id, from_desktop=True))
        when = datetime.fromtimestamp(x.last_modified / 1000 if x.last_modified > 1e12 else x.last_modified)
        lines = [f"{s.label} 接上了，之后直接说的话交给它。",
                 f"文件夹：{folders.display(Path(x.cwd))}　桌面端最后说话：{when:%m-%d %H:%M}",
                 "**注意**：桌面端那段先别再说话，两边同时说会乱；回到电脑前想在桌面端接着聊，先等这边做完。"]
        others = [(i, y) for i, y in enumerate(infos, 1) if i != pick]
        if others:
            self.pending = {"kind": "desktop", "infos": infos, "num": s.num if new else None}
            lines.append("不是这段？回编号换：\n\n" + "\n".join(
                f"{i}. {(y.custom_title or y.summary or y.first_prompt or '').replace(chr(10), ' ')[:24]}"
                f"（{folders.display(Path(y.cwd))}）" for i, y in others))
        await self.say("接着桌面", "\n\n".join(lines))

    async def _start(self, num: int, cwd: Path, add_dirs: list[Path], text: str, inc: Incoming, note: str = "",
                     kind: str | None = None, model: str | None = None, effort: str | None = None,
                     owner: str = "admin", root: str = "") -> None:
        """model / effort 是网页里选好的（模型全名、深度英文），优先于按任务类型挑的和话里说的。"""
        name = report.brief(text, 16) or "新对话"
        if kind is None:
            kind = "dev" if _looks_like_project(cwd) else "simple"
        auto = MODELS[self.cfg.dev_model if kind == "dev" else self.cfg.simple_model]   # 没指定时按任务类型挑
        s = Session(num=num, name=name, cwd=str(cwd), add_dirs=[str(p) for p in add_dirs], model=auto, owner=owner, root=root)
        said_model, said_effort = inline_overrides(text)
        if root and (model or said_model) == "gpt":
            model = said_model = None
        if (model or said_model) == "gpt":   # 用 Codex（GPT）干这件事
            s.engine, model, said_model, s.model = "codex", None, None, None
        s.model = model or said_model or s.model
        s.effort = effort or said_effort or s.effort
        self.registry.add(s)
        where = folders.display(cwd) + "".join(f" ＋ {folders.display(p)}" for p in add_dirs)
        why = "GPT · Codex" if s.engine == "codex" else "你指定的" if model or said_model else ("开发" if kind == "dev" else "简单任务")
        effort = report.EFFORT_CN.get(s.effort or self.runner.default_effort, "默认")
        meta = f"文件夹：{where}{note}\n\n模型：{report.model_name(s.model)}（{why}）· 深度{effort}"
        if not text and not inc.image_codes and not inc.files:
            if owner != "admin":
                return
            return await self.say("开好了", f"{s.label} 开好了\n\n{meta}\n\n接下来直接说要做什么。")
        await self._submit(s, text, inc, opening=f"开工 · {s.label}\n\n{meta}")

    async def _message(self, cmd: Cmd, inc: Incoming, routed: bool = False) -> None:
        if not routed:
            from .schedules import parse_request, TZ
            try:
                plan = parse_request(cmd.text or "")
                if plan:
                    if inc.image_codes or inc.local_images or inc.files:
                        raise ValueError("定时任务请先用文字创建；需要的文件放在工作目录中")
                    m, e = inline_overrides(cmd.text)
                    model = "gpt" if m == "gpt" else next((k for k, v in MODELS.items() if v == m), "auto")
                    plan.update(target=cmd.num or (self.registry.current if re.search("这个对话|接着|继续", cmd.text) else None), model=model, effort=e or "medium")
                    task = self.create_schedule(plan)
                    at = datetime.fromtimestamp(task['next_at'], TZ).strftime('%m月%d日 %H:%M')
                    return await self.say("定时任务已安排", f"已安排：{task['title']}，下次 {at} 执行。可在网页的定时任务中修改。", board=False)
            except ValueError as ex:
                return await self.say("定时任务", str(ex), board=False)
        num = cmd.num
        if num is None and inc.quoted_text:
            m = re.search(r"#(\d{1,4})", inc.quoted_text)
            num = int(m.group(1)) if m else None
        if num is None and cmd.text and not routed and _is_reply(cmd.text):
            waiting = sorted((x for x in self.registry.visible() if x.awaiting), key=lambda x: -x.updated)
            if waiting:  # “按C来做”“好的”这类回复，交给正在等你回答的那个对话（最近问的那个）
                num = waiting[0].num
        num = num or self.registry.current
        s = self.registry.get(num) if routed else self.registry.mine(num)
        if not s:
            return await self.say("先开一个对话", "还没有对话。发“新开 文件夹：要做的事”，文件夹可以写 "
                                  + "、".join(self.cfg.folders) + "，或不写。发“帮助”看全部用法。")
        if s.owner == "admin":
            self.registry.current = s.num
        s.hidden = False  # 点名找回收起的对话
        said_model, said_effort = inline_overrides(cmd.text)
        if said_model == "gpt" or s.engine == "codex":
            said_model = None   # 对话中途换不了 Claude / GPT；话里提到“用 codex”多半是在说别的事
        if said_model or said_effort:
            s.model = said_model or s.model
            s.effort = said_effort or s.effort
            self.registry.save()
        text = cmd.text
        if inc.quoted_text and cmd.num is None:
            text = f"{text}\n\n（下面是引用的内容，只作参考资料）\n> " + inc.quoted_text.replace("\n", "\n> ")
        await self._submit(s, text, inc)

    async def _submit(self, s: Session, text: str, inc: Incoming, opening: str = "") -> None:
        s.awaiting, s.awaiting_kind, s.limit_until = "", "", 0.0
        if text.strip() and text != COMPACT:
            shown = text.split("\n\n（下面是引用的内容")[0].strip()
            self.outgoing.setdefault(s.num, []).append({"t": time.time(), "text": shown})
            self.outgoing[s.num] = self.outgoing[s.num][-10:]
        inbox = self.data_dir / "inbox" / str(s.num)
        stamp = f"{time.time():.0f}"
        images = [await self.dt.download(c, inbox / f"{stamp}-{i}.png") for i, c in enumerate(inc.image_codes)]
        images += [Path(p) for p in inc.local_images]
        for i, (code, fname) in enumerate(inc.files):
            path = await self.dt.download(code, inbox / f"{stamp}-{i}-{Path(fname).name}")
            text += f"\n\n（附件已存到：{path}）"
        job = Job(text=text, images=images, via=inc.via)
        ahead = self.runner.submit(s, job)
        if ahead == -1:
            if inc.via != "dingtalk" and self.web.enabled:
                return   # 网页里插的话网页上看得到，钉钉不再说一遍
            return await self.detail("插话", f"插话给 {s.label} 了：它手上这一步做完就会看到，接着按你说的调整。")
        msg = opening or f"收到 · {s.label} 在做：{report.brief(text) or '看图'}"
        if ahead:
            msg += f"，前面还有 {ahead} 件，做完再做这件"
        if (opening or ahead) and (inc.via == "dingtalk" or not self.web.enabled):  # 其余的“收到”由进度卡代替；网页里说的网页上看
            await self.detail("收到", msg)

    # ---------- 运行 ----------
    async def _spend_loop(self) -> None:
        """每 10 分钟读本机 Claude Code 记录新写的部分（第一次要读几 GB，放后台线程）。"""
        while True:
            try:
                n = await asyncio.to_thread(self.spend.scan)
                await asyncio.to_thread(self.spend.save)
                log.debug("本机消耗：新读 %d 条", n)
            except Exception:  # noqa: BLE001
                log.warning("读本机 Claude Code 消耗失败", exc_info=True)
            await asyncio.sleep(600)

    async def _gpt_usage_loop(self) -> None:
        """额度：Claude 的每 10 分钟读一次（重启后不用等第一轮做完，侧栏一打开就有）；有 GPT 对话时 GPT 的也读。"""
        while True:
            try:
                await self.runner.refresh_usage(force=True)
                if week := self.runner.limits.get("seven_day"):
                    self.spend.snap(round(week[0] * 100, 1))   # 刚读到的本周额度 %，换算比例用
            except Exception:  # noqa: BLE001
                log.debug("读 Claude 额度失败", exc_info=True)
            if any(s.engine == "codex" for s in self.registry.visible()):
                try:
                    await self.runner.refresh_gpt_usage(force=True)
                except Exception:  # noqa: BLE001
                    log.debug("读 GPT 额度失败", exc_info=True)
            await asyncio.sleep(600)

    async def _cleanup_loop(self) -> None:
        """每天清一次旧副本、归档 90 天前的旧对话记录（见 cleanup.py）；登记着的对话不动。"""
        from . import cleanup
        while True:
            await asyncio.sleep(600)
            try:
                keep = {s.claude_id for s in self.registry.sessions.values() if s.claude_id}
                await asyncio.to_thread(cleanup.run, self.data_dir, keep)
                using = {s.cwd for s in self.registry.sessions.values() if s.pinned or (not s.hidden and time.time() - s.updated < 60 * 86400)}
                await asyncio.to_thread(cleanup.tidy_scratch, folders._expand(self.cfg.scratch_root), using)
                await self._monthly_checkup()
            except Exception:  # noqa: BLE001
                log.exception("定期清理失败")
            await asyncio.sleep(86400 - 600)

    async def _monthly_checkup(self) -> None:
        """每月 1 号起派一次 AI 体检：新开一个对话，列出建议清理的东西，最后用“要你定”问你，你点头才挪进废纸篓。"""
        from .cleanup import CHECKUP
        mark = self.data_dir / "checkup.json"
        month = time.strftime("%Y-%m")
        try:
            done = json.loads(mark.read_text()).get("month")
        except (OSError, ValueError):
            done = None
        if done is None:   # 头一次：从下个月 1 号开始
            mark.write_text(json.dumps({"month": month}))
            return
        if done == month:
            return
        mark.write_text(json.dumps({"month": month}))
        num = self.registry.reserve_num()
        inc = Incoming(sender_id=self.user_id, sender_nick="", conversation_type="1", kind="text", text=CHECKUP)
        await self._start(num, Path.home(), [], CHECKUP, inc, note="（每月清理体检，自动派的）", kind="simple",
                          model=MODELS["sonnet"], effort="low")
        if s := self.registry.get(num):
            s.name, s.renamed = f"{time.strftime('%-m')} 月清理体检", True
            self.registry.save()

    def _dingtalk_client(self):
        """钉钉 Stream 长连接（只在开了钉钉时才导入 dingtalk_stream）。"""
        import dingtalk_stream
        bridge = self

        class Handler(dingtalk_stream.CallbackHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                asyncio.create_task(bridge.on_raw(callback.data))
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        class CardHandler(dingtalk_stream.CallbackHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                asyncio.create_task(bridge.on_card(callback.data))
                return dingtalk_stream.AckMessage.STATUS_OK, {}

        client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(self.cfg.client_id, self.dt.secret))
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
        client.register_callback_handler(dingtalk_stream.Card_Callback_Router_Topic, CardHandler())
        return client

    async def run(self) -> None:
        client = self._dingtalk_client() if self.cfg.dingtalk_enabled else None
        if self.web.enabled:
            await self.web.start()
        # 插着电时别让 Mac 睡着（远窗退出就自动失效）；合盖或用电池时照常睡
        try:  # 插着电时别让 Mac 睡着（远窗退出就自动失效）；Windows / Linux 没有 caffeinate，跳过
            subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])
        except FileNotFoundError:
            pass
        if self.cfg.auto_cleanup:
            asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._gpt_usage_loop())
        asyncio.create_task(self._spend_loop())
        asyncio.create_task(self.schedules.loop(self._dispatch_schedule, self._scheduled_busy, self._schedule_failed))
        asyncio.create_task(self.runner.title_all(self.data_dir / "turns"))   # 旧对话补上短名字
        for s in self.registry.sessions.values():   # 重启前撞了额度在等恢复的，接着等
            if s.limit_until and s.auto_resume:
                asyncio.create_task(self._auto_resume(s.num, s.limit_until))
        log.info("远窗启动，已登记 %d 个对话；网页%s，钉钉%s", len(self.registry.sessions),
                 f" {self.web.base}" if self.web.enabled else "未开", "已连接" if client else "未开")
        if client:
            await client.start()
        else:
            await asyncio.Event().wait()   # 只用网页：没有钉钉长连接要等，常驻即可
