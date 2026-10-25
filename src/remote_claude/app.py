"""钉钉桥本体：收到你的消息 → 认人 → 解析指令或找对话 → 交给 Claude → 把结果推回钉钉。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import dingtalk_stream
from claude_agent_sdk import list_sessions

from . import folders, report, router, translate
from .commands import HELP, Cmd, parse
from .config import DATA_DIR, Config
from .dingtalk import DingTalk, Incoming, parse_incoming
from .report import Turn
from .sessions import Job, Registry, Runner, Session, describe, user_setting

MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
EFFORTS = {"低": "low", "中": "medium", "高": "high", "很高": "xhigh", "最高": "max", "默认": None}

CHAT_MEMORY = 30 * 60  # 小E 直接回答的话记 30 分钟，你接着问它还接得上

log = logging.getLogger(__name__)


_REPLY = re.compile(r"(好的?|好吧|行|可以|对|是的?|嗯+|ok|OK|继续|接着|就这样|选?[A-Da-d1-9]|再.{0,6}一点|为什么|然后呢|还有呢)[。！!？?…，,]*")


def inline_overrides(text: str) -> tuple[str | None, str | None]:
    """话里明说了模型或深度（“用 opus”“深度调到高”），就以你说的为准。"""
    model = effort = None
    if m := re.search(r"(?:用|换成|改用|切到|模型)\s*(opus|sonnet|haiku|fable)", text, re.I):
        model = MODELS[m.group(1).lower()]
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
    """一看就是对上一条的回复，不必请小E判断，直接交给当前对话（省一次调用）。"""
    t = text.strip()
    return bool(_REPLY.fullmatch(t)) or len(t) <= 3


class Bridge:
    def __init__(self, cfg: Config, dt: DingTalk, data_dir: Path = DATA_DIR):
        self.cfg = cfg
        self.dt = dt
        self.data_dir = data_dir
        self.registry = Registry(data_dir / "sessions.json")
        self.runner = Runner(self.registry, self._turn_done, cfg.max_parallel, cfg.default_effort)
        self.pending: dict | None = None  # 等你回编号选文件夹 / 选最近的对话
        self.photo_buf: Incoming | None = None  # 只发了图、还没跟话时先攒着，等后面那句话一起交
        self.photo_task: asyncio.Task | None = None
        self.photo_wait = 15.0
        self.receipt_delay = 4.0
        self.chat: list[tuple[float, str, str]] = []  # 小E 自己直接回过的几轮（时间、你说的、它答的），接话时带上
        self.chat_at = 0.0  # 最近一次小E 直接回答的时间
        self.work_at = 0.0  # 最近一次把话交给对话的时间
        self.user_id = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else ""
        for s in self.registry.sessions.values():
            if s.status in ("running", "queued"):
                s.status = "error"
        self.registry.save()

    # ---------- 发消息 ----------
    async def say(self, title: str, text: str) -> None:
        for i, part in enumerate(report.split(text)):
            await self.dt.send_markdown(self.user_id, title if i == 0 else f"{title}（续）", part)

    async def _turn_done(self, s: Session, turn: Turn) -> None:
        if translate.needs_translation(turn.final_text):
            if zh := await translate.to_chinese(turn.final_text):
                turn.final_text, turn.translated = zh, True
        title, body = report.build(s.label, turn, s.cwd, self.runner.limits)
        s.last_title = title
        self.registry.save()
        try:
            await self.say(title, body)
            for img in turn.images:
                await self.dt.send_image(self.user_id, Path(img))
        except Exception:
            log.exception("推送 #%s 的结果失败", s.num)

    # ---------- 收消息 ----------
    async def on_raw(self, data: dict) -> None:
        inc = parse_incoming(data)
        if inc.sender_id not in self.cfg.allowed_user_ids or inc.conversation_type != "1":
            log.warning("忽略一条消息：发送人 %s（%s），会话类型 %s", inc.sender_id, inc.sender_nick, inc.conversation_type)
            return
        log.info("收到 %s，文字 %d 字，图片 %d，文件 %d，引用 %s", inc.kind, len(inc.text), len(inc.image_codes),
                 len(inc.files), "有" if inc.quoted_text else "无")
        try:
            await self.handle(inc)
        except Exception as e:  # noqa: BLE001
            log.exception("处理消息失败")
            await self.say("出错了", f"小E 处理这条消息时出错：{type(e).__name__}: {str(e)[:200]}")

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
        if cmd.kind == "quota":
            return await self._quota()
        if cmd.kind == "recent":
            return await self._recent(cmd.folders[0])
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
        s.hidden = True
        if self.registry.current == s.num:
            self.registry.current = None
        self.registry.save()
        await self.say("已收起", f"{s.label} 收起了：不再出现在列表里，小E 也不会再把话交给它。"
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
                   note: str = "", kind: str | None = None) -> None:
        resolved = list(resolved or [])
        rest = tokens[len(resolved):]
        if not tokens:
            num = self.registry.reserve_num()
            cwd = folders.scratch_dir(self.cfg, num)
            return await self._start(num, cwd, [], text, inc, note=note or "（没指定文件夹，用了临时文件夹）", kind=kind)
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
        await self._start(num, resolved[0], resolved[1:], text, inc, note=note, kind=kind)

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
        elif p["kind"] == "recent":
            await self._recent(p["folder"], pick=n)
        elif p["kind"] == "route":
            if n == 1:
                await self._message(Cmd("message", num=p["cur"], text=p["cmd"].text), p["inc"], routed=True)
            elif n == 2:
                await self._new([p["folder"]] if p["folder"] else [], p["cmd"].text, p["inc"])
            else:
                await self.say("接着还是新开", "编号不对，这句先没交出去，请重新发一次。")

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
        s = self.registry.add(Session(num=num, name=name, cwd=str(d), claude_id=x.session_id))
        await self.say("接上了", f"{s.label} 接上了，之后直接说的话交给它。")

    async def _start(self, num: int, cwd: Path, add_dirs: list[Path], text: str, inc: Incoming, note: str = "",
                     kind: str | None = None) -> None:
        name = report.brief(text, 16) or "新对话"
        if kind is None:
            kind = "dev" if _looks_like_project(cwd) else "simple"
        model = MODELS[self.cfg.dev_model if kind == "dev" else self.cfg.simple_model]
        s = Session(num=num, name=name, cwd=str(cwd), add_dirs=[str(p) for p in add_dirs], model=model)
        said_model, said_effort = inline_overrides(text)
        s.model = said_model or s.model
        s.effort = said_effort or s.effort
        self.registry.add(s)
        where = folders.display(cwd) + "".join(f" ＋ {folders.display(p)}" for p in add_dirs)
        why = "你指定的" if said_model else ("开发" if kind == "dev" else "简单任务")
        effort = report.EFFORT_CN.get(s.effort or self.runner.default_effort, "默认")
        meta = f"文件夹：{where}{note}\n\n模型：{report.model_name(s.model)}（{why}）· 深度{effort}"
        if not text and not inc.image_codes and not inc.files:
            return await self.say("开好了", f"{s.label} 开好了\n\n{meta}\n\n接下来直接说要做什么。")
        await self._submit(s, text, inc, opening=f"开工 · {s.label}\n\n{meta}")

    async def _message(self, cmd: Cmd, inc: Incoming, routed: bool = False) -> None:
        num = cmd.num
        if num is None and inc.quoted_text:
            m = re.search(r"#(\d{1,4})", inc.quoted_text)
            num = int(m.group(1)) if m else None
        chatting = self.chat_at > self.work_at and time.time() - self.chat_at < CHAT_MEMORY
        if num is None and cmd.text and not routed and self.cfg.auto_route and (chatting or not _is_reply(cmd.text)):
            recent = [(q, a) for t, q, a in self.chat if time.time() - t < CHAT_MEMORY]
            r = await router.route(cmd.text, self.registry.visible(), self.registry.current,
                                   list(self.cfg.folders), chat=recent)
            if r and r.action == "reply":
                self.chat = [*self.chat[-5:], (time.time(), cmd.text, r.answer)]
                self.chat_at = time.time()
                return await self.say("回复", f"{r.answer}\n\n---\n\n小E 直接回的（Haiku），没占用任何对话")
            if r and r.action == "new":
                if r.folder and not folders.candidates(r.folder, self.cfg):
                    r.action = "ask"
                else:
                    return await self._new([r.folder] if r.folder else [], cmd.text, inc, note="（判断是件新事，自动新开）",
                                           kind=r.kind)
            if r and r.action == "ask":
                return await self._ask_route(cmd, inc, r.folder)
            if r and r.action == "continue":
                num = r.target
        num = num or self.registry.current
        s = self.registry.get(num)
        if not s:
            return await self.say("先开一个对话", "还没有对话。发“新开 文件夹：要做的事”，文件夹可以写 "
                                  + "、".join(self.cfg.folders) + "，或不写。发“帮助”看全部用法。")
        self.registry.current = s.num
        s.hidden = False  # 点名找回收起的对话
        said_model, said_effort = inline_overrides(cmd.text)
        if said_model or said_effort:
            s.model = said_model or s.model
            s.effort = said_effort or s.effort
            self.registry.save()
        text = cmd.text
        if inc.quoted_text and cmd.num is None:
            text = f"{text}\n\n（下面是引用的内容，只作参考资料）\n> " + inc.quoted_text.replace("\n", "\n> ")
        await self._submit(s, text, inc)

    async def _ask_route(self, cmd: Cmd, inc: Incoming, folder: str | None) -> None:
        cur = self.registry.get(self.registry.current)
        where = f"在 {folder} 里" if folder and folders.candidates(folder, self.cfg) else "不指定文件夹"
        if not cur:
            return await self._new([folder] if where != "不指定文件夹" else [], cmd.text, inc)
        self.pending = {"kind": "route", "cmd": cmd, "inc": inc, "cur": cur.num,
                        "folder": folder if where != "不指定文件夹" else None}
        await self.say("接着还是新开", f"这句要接着 {cur.label}，还是新开一个？回编号：\n\n1. 接着 {cur.label}\n2. 新开（{where}）")

    async def _submit(self, s: Session, text: str, inc: Incoming, opening: str = "") -> None:
        inbox = self.data_dir / "inbox" / str(s.num)
        stamp = f"{time.time():.0f}"
        images = [await self.dt.download(c, inbox / f"{stamp}-{i}.png") for i, c in enumerate(inc.image_codes)]
        for i, (code, fname) in enumerate(inc.files):
            path = await self.dt.download(code, inbox / f"{stamp}-{i}-{Path(fname).name}")
            text += f"\n\n（附件已存到：{path}）"
        job = Job(text=text, images=images)
        self.work_at = time.time()
        ahead = self.runner.submit(s, job)
        msg = opening or f"收到 · {s.label} 在做：{report.brief(text) or '看图'}"
        if ahead:
            msg += f"，前面还有 {ahead} 件，做完再做这件"
        if opening or ahead:
            await self.say("收到", msg)
        else:
            asyncio.create_task(self._receipt_later(job, msg))

    async def _receipt_later(self, job: Job, msg: str) -> None:
        """几秒内就答完的（闲聊、简单问答）不发“收到”，免得刷屏。"""
        await asyncio.sleep(self.receipt_delay)
        if not job.finished:
            await self.say("收到", msg)

    # ---------- 运行 ----------
    async def run(self) -> None:
        bridge = self

        class Handler(dingtalk_stream.CallbackHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                asyncio.create_task(bridge.on_raw(callback.data))
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        cred = dingtalk_stream.Credential(self.cfg.client_id, self.dt.secret)
        client = dingtalk_stream.DingTalkStreamClient(cred)
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
        log.info("小E 启动，已登记 %d 个对话", len(self.registry.sessions))
        await client.start()
