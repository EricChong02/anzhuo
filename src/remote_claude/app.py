"""钉钉桥本体：收到你的消息 → 认人 → 解析指令或找对话 → 交给 Claude → 把结果推回钉钉。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import dingtalk_stream
from claude_agent_sdk import list_sessions

from . import cards, folders, report, router, translate
from .commands import HELP, Cmd, parse
from .config import DATA_DIR, Config, read_web_hash
from .dingtalk import DingTalk, Incoming, parse_incoming
from .report import Turn
from .web import Web
from .sessions import Job, Registry, Runner, Session, describe, user_setting

MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
EFFORTS = {"低": "low", "中": "medium", "高": "high", "很高": "xhigh", "最高": "max", "默认": None}

RECENT_KEEP = 100  # recent.jsonl 只留最近这么多行（你发的、小E 发的、分给了谁）
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
        self.runner = Runner(self.registry, self._turn_done, cfg.max_parallel, cfg.default_effort, self._turn_started)
        self.board: str | None = None  # 工作台卡：所有在做的对话一张卡，始终挪到聊天最下面
        self.board_task: asyncio.Task | None = None
        self.board_lock = asyncio.Lock()
        self.board_shown = ""
        self.progress_every = (15.0, 45.0)  # 工作台多久更新一次：都在前 5 分钟 / 有做得久的（省钉钉调用次数）
        self.web = Web(cfg.web_base, cfg.web_port, data_dir, cfg.preview_upstream, cfg.preview_root,
                       cfg.web_user, read_web_hash(cfg.web_user) if cfg.web_base else "")
        self.cards_path = data_dir / "cards.json"
        self.cards: dict[str, dict] = self._load_cards()  # 卡片 → 它属于哪个对话、是什么卡；重启后按钮还能用
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
    def record(self, who: str, text: str, **extra) -> None:
        """最近的来往原文存一份在本机（你同意的），排查“小E 为什么这么答”时看原话。只留最近 100 行。"""
        try:
            path = self.data_dir / "recent.jsonl"
            lines = path.read_text().splitlines()[-(RECENT_KEEP - 1):] if path.exists() else []
            row = {"t": time.strftime("%m-%d %H:%M:%S"), "who": who, "text": text[:2000], **extra}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join([*lines, json.dumps(row, ensure_ascii=False)]) + "\n")
        except OSError:
            log.debug("记最近来往失败", exc_info=True)

    async def say(self, title: str, text: str, board: bool = True) -> None:
        self.record("小E", text, title=title)
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
        """小E 刚发了别的消息：还有活在做，就把工作台挪到它下面，免得被顶上去看不到；都做完了就当场收尾。"""
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
        """几秒内答完的不打扰；做得久的上工作台（没有就发一张）。"""
        await asyncio.sleep(self.receipt_delay)
        if job.finished or turn.ended:
            return
        if self.board:
            return  # 工作台自己会刷新出这一件
        try:
            await self._board_post()
        except Exception:  # noqa: BLE001 - 卡片发不出就退回一句文字
            log.warning("发工作台失败", exc_info=True)
            await self.say("收到", f"收到 · {s.label} 在做：{turn.request or '看图'}", board=False)

    async def _turn_done(self, s: Session, turn: Turn) -> None:
        if translate.needs_translation(turn.final_text):
            if done := await translate.to_chinese(turn.final_text):
                turn.final_text, turn.translated = done
        title, body = report.build(s.label, turn, s.cwd, self.runner.limits)
        if self.web.enabled and (turn.changed_files or turn.commands or turn.images or turn.is_error):
            try:
                tid = self.web.save_turn(s.label, turn, s.cwd, title.split(" · ")[0])
                turn.links = [("完整过程", self.web.link(f"/_x/t/{tid}"))]
                turn.links += [(f"试课：{name}", self.web.link(url))
                               for name, url in self.web._turn(tid).get("previews", [])]
                title, body = report.build(s.label, turn, s.cwd, self.runner.limits)
            except Exception:  # noqa: BLE001 - 过程页出问题不影响报告
                log.exception("记过程页失败")
        s.last_title = title
        s.last_summary = f"你说“{turn.request}”，它答“{report.brief(turn.final_text, 60)}”"
        self.registry.save()
        try:
            await self.say(title, body, board=False)
            for img in turn.images:
                await self.dt.send_image(self.user_id, Path(img))
            if not turn.is_error and (d := cards.parse_decision(turn.final_text)):
                oid = f"d{s.num}-{int(time.time() * 1000)}"
                await self.dt.create_card(self.user_id, oid, cards.decision_data(s.label, d))
                self.cards[oid] = {"kind": "decision", "num": s.num, "question": d.question,
                                   "options": d.options, "recommend": d.recommend}
                self._save_cards()
        except Exception:
            log.exception("推送 #%s 的结果失败", s.num)
        await self._board_follow()

    async def _reply(self, answer: str, text: str, inc: Incoming) -> None:
        """小E 直接回答；有当前对话时附一个“交给 #N”按钮，判断错了点一下就转过去。"""
        cur = self.registry.get(self.registry.current)
        if not cur or cur not in self.registry.visible():
            return await self.say("回复", f"{answer}\n\n---\n\n小E 直接回的（Haiku），没占用任何对话")
        oid = f"r{cur.num}-{int(time.time() * 1000)}"
        try:
            await self.dt.create_card(self.user_id, oid, cards.reply_data(answer, cur.label))
        except Exception:  # noqa: BLE001
            log.warning("发回复卡失败", exc_info=True)
            return await self.say("回复", f"{answer}\n\n---\n\n小E 直接回的（Haiku）。要说给 {cur.label}，发“#{cur.num} 原话”")
        self.record("小E", answer, title="回复卡", forward_to=cur.num)
        self.cards[oid] = {"kind": "reply", "num": cur.num, "text": text, "answer": answer}
        self._save_cards()
        await self._board_follow()

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
        if action == "fwd" and info["kind"] == "reply":
            if info.get("done"):
                return
            info["done"] = True
            self._save_cards()
            self.chat = [c for c in self.chat if c[1] != info["text"]]  # 这句不算小E 的闲聊了
            await self.dt.update_card(oid, cards.reply_data(info["answer"], s.label, forwarded=True))
            inc = Incoming(sender_id=self.user_id, sender_nick="", conversation_type="1", kind="text", text=info["text"])
            await self._message(Cmd("message", num=s.num, text=info["text"]), inc, routed=True)
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
        if cmd.kind == "web":
            if not self.web.enabled:
                return await self.say("网页", "网页还没开通（配置里没有 web_base）。")
            return await self.say("网页", f"小E 网页：{self.web.base}\n\n用账号密码登录，一次登录 30 天免登录。"
                                          "里面有在做的课（一点就试课）和最近每一轮的完整过程。")
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
        elif p["kind"] == "desktop":
            await self._desktop(n, p["infos"], replace=p["num"])
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
        s = self.registry.add(Session(num=num, name=name, cwd=str(d), claude_id=x.session_id, from_desktop=True))
        await self.say("接上了", f"{s.label} 接上了，之后直接说的话交给它。")

    async def _desktop(self, pick: int | None, infos: list | None = None, replace: int | None = None) -> None:
        """接上你最近在桌面端聊的那段：不用记文件夹和编号。小E 自己新开的、临时文件夹里的不算。"""
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
            busy = {n for n in self.registry.sessions if self.runner.busy(n)}
            r = await router.route(cmd.text, self.registry.visible(), self.registry.current,
                                   list(self.cfg.folders), chat=recent, busy=busy)
            self.record("分派", r.reason if r else "判断失败", action=r.action if r else None,
                        target=r.target if r else None)
            if r and r.action == "reply" and (inc.image_codes or inc.files):
                r = None  # 小E 看不到图和文件，带图的话交给当前对话
            if r and r.action == "reply":
                self.chat = [*self.chat[-5:], (time.time(), cmd.text, r.answer)]
                self.chat_at = time.time()
                return await self._reply(r.answer, cmd.text, inc)
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
        if ahead == -1:
            return await self.say("插话", f"插话给 {s.label} 了：它手上这一步做完就会看到，接着按你说的调整。")
        msg = opening or f"收到 · {s.label} 在做：{report.brief(text) or '看图'}"
        if ahead:
            msg += f"，前面还有 {ahead} 件，做完再做这件"
        if opening or ahead:  # 其余的“收到”由进度卡代替（几秒内答完的不发）
            await self.say("收到", msg)

    # ---------- 运行 ----------
    async def run(self) -> None:
        bridge = self

        class Handler(dingtalk_stream.CallbackHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                asyncio.create_task(bridge.on_raw(callback.data))
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        class CardHandler(dingtalk_stream.CallbackHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                asyncio.create_task(bridge.on_card(callback.data))
                return dingtalk_stream.AckMessage.STATUS_OK, {}

        cred = dingtalk_stream.Credential(self.cfg.client_id, self.dt.secret)
        client = dingtalk_stream.DingTalkStreamClient(cred)
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
        client.register_callback_handler(dingtalk_stream.Card_Callback_Router_Topic, CardHandler())
        if self.web.enabled:
            await self.web.start()
        log.info("小E 启动，已登记 %d 个对话", len(self.registry.sessions))
        await client.start()
