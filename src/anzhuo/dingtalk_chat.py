"""钉钉里收消息：认人、攒图、解析文字指令（列表、收起、换模型、额度、新建文件夹、接最近 / 桌面端的对话……），
需要你回编号时记住在等什么。普通的话交回 Bridge.message 去分派；网页不走这里。"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import list_sessions

from . import engines, folders, report
from .commands import HELP, Cmd, parse
from .dingtalk import Incoming, parse_incoming
from .engines import EFFORTS, MODELS
from .sessions import Session, describe, user_setting

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger(__name__)


class DingChat:
    def __init__(self, bridge: Bridge):
        self.b = bridge
        self.pending: dict | None = None  # 等你回编号：选文件夹 / 选最近的对话 / 接着还是新开
        self.photo_buf: Incoming | None = None  # 只发了图、还没跟话时先攒着，等后面那句话一起交
        self.photo_task: asyncio.Task | None = None
        self.photo_wait = 15.0

    async def on_raw(self, data: dict) -> None:
        b = self.b
        inc = parse_incoming(data)
        if inc.sender_id not in b.cfg.allowed_user_ids or inc.conversation_type != "1":
            log.warning("忽略一条消息：发送人 %s（%s），会话类型 %s", inc.sender_id, inc.sender_nick, inc.conversation_type)
            return
        log.info("收到 %s，文字 %d 字，图片 %d，文件 %d，引用 %s", inc.kind, len(inc.text), len(inc.image_codes),
                 len(inc.files), "有" if inc.quoted_text else "无")
        b.record("你", inc.text, kind=inc.kind, images=len(inc.image_codes), files=len(inc.files),
                 quoted=inc.quoted_text[:200])
        try:
            await self.handle(inc)
        except Exception as e:  # noqa: BLE001
            log.exception("处理消息失败")
            await b.say("出错了", f"远窗处理这条消息时出错：{type(e).__name__}: {str(e)[:200]}")

    async def handle(self, inc: Incoming) -> None:
        b = self.b
        say = b.say
        if inc.kind == "audio" and not inc.text:
            return await say("没听清", "这条语音没拿到文字。可以用键盘上的麦克风听写，说完再发。")
        if inc.kind not in ("text", "picture", "richText", "audio", "file"):
            return await say("看不懂", f"这种消息（{inc.kind}）我还处理不了，请发文字、图片或文件。")
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
        if getattr(cmd, "num", None) and cmd.kind != "pick" and not b.registry.mine(cmd.num):
            cmd.num = -1   # 别的账号的对话：钉钉里当作没有
        if cmd.kind == "pick" and self.pending:
            return await self.pick(cmd.num)
        if cmd.kind == "pick":
            cmd = Cmd("message", text=inc.text)
        self.pending = None if cmd.kind != "message" else self.pending

        if cmd.kind == "help":
            return await say("用法", HELP)
        if cmd.kind == "list":
            return await self._list(show_all="全部" in cmd.text)
        if cmd.kind == "delete":
            return await self._delete(cmd.num)
        if cmd.kind == "switch":
            s = b.registry.get(cmd.num)
            if not s:
                return await say("没有这个对话", f"没有 #{cmd.num}。发“列表”看看有哪些。")
            b.registry.current = s.num
            b.registry.save()
            return await say("已切换", f"之后直接说的话交给 {s.label}。")
        if cmd.kind == "stop":
            s = b.registry.get(cmd.num or b.registry.current)
            if not s:
                return await say("没有这个对话", "没找到要停的对话。")
            stopped = await b.runner.stop(s.num)
            return await say("停", f"{s.label}：{'已让它停下，稍后发停下时的情况' if stopped else '本来就没在做，排队的也清掉了'}。")
        if cmd.kind == "rename":
            s = b.registry.get(cmd.num)
            if not s:
                return await say("没有这个对话", f"没有 #{cmd.num}。")
            s.name = cmd.name[:30]
            b.registry.save()
            return await say("已改名", s.label)
        if cmd.kind in ("model", "effort"):
            return await self._set_model(cmd)
        if cmd.kind == "web":
            if not b.web.enabled:
                return await say("网页", "网页没有开启（配置里 web = false）。")
            return await say("网页", f"远窗网页：{b.web.base}\n\n用账号密码登录，一次登录 30 天免登录。"
                                     "里面有所有对话和最近每一轮的完整过程。")
        if cmd.kind == "compact":
            return await b.compact(cmd.num)
        if cmd.kind == "quota":
            return await self._quota()
        if cmd.kind == "recent":
            return await self._recent(cmd.folders[0])
        if cmd.kind == "desktop":
            return await self._desktop(cmd.num)
        if cmd.kind == "resume":
            return await self._recent(cmd.folders[0], pick=cmd.num)
        if cmd.kind == "new":
            return await b.new_session(cmd.folders, cmd.text, inc)
        if cmd.kind == "mkdir":
            return await self._mkdir(cmd.folders[0] if cmd.folders else "", cmd.text, inc)
        await b.message(cmd, inc)

    async def _flush_photos(self) -> None:
        await asyncio.sleep(self.photo_wait)
        inc, self.photo_buf = self.photo_buf, None
        if inc:
            await self.b.message(Cmd("message"), inc)

    # ---------- 指令 ----------
    async def _list(self, show_all: bool = False) -> None:
        b = self.b
        if not b.registry.sessions:
            return await b.say("对话列表", "还没有对话。发“新开 文件夹：要做的事”开一个，发“帮助”看用法。")
        visible = b.registry.visible()
        shown = list(b.registry.sessions.values()) if show_all else visible
        rows = []
        for s in sorted(shown, key=lambda x: -x.updated)[:30 if show_all else 15]:
            mark = "（当前）" if s.num == b.registry.current else ""
            mark += "（收起）" if s not in visible else ""
            rows.append(f"- {describe(s, s.num in b.runner.turns)}{mark}")
        hidden = len(b.registry.sessions) - len(visible)
        if not rows:
            rows.append("最近 7 天没有在动的对话。")
        if hidden and not show_all:
            rows.append(f"\n另有 {hidden} 个收起了（7 天没动或删掉的），发“列表 全部”看；发“#编号 …”可以接着聊。")
        await b.say("对话列表", "\n".join(rows))

    async def _delete(self, num: int | None) -> None:
        b = self.b
        s = b.registry.get(num)
        if not s:
            return await b.say("没有这个对话", f"没有 #{num}。发“列表”看看有哪些。")
        if s.num in b.runner.turns or s.status in ("running", "queued"):
            return await b.say("还在做", f"{s.label} 还在做，先发“停 {s.num}”，停下后再删。")
        s.hidden, s.hidden_at, s.pinned = True, time.time(), False
        if b.registry.current == s.num:
            b.registry.current = None
        b.registry.save()
        await b.say("已收起", f"{s.label} 收起了：不再出现在列表里，远窗也不会再把话交给它。"
                              f"聊天记录还在，要找回发“#{s.num} 接着说的话”。")

    async def _set_model(self, cmd: Cmd) -> None:
        b = self.b
        s = b.registry.get(cmd.num or b.registry.current)
        if not s:
            return await b.say("没有这个对话", "先开一个对话，或写上编号，比如“模型 7 sonnet”。")
        if cmd.kind == "model":
            if not cmd.name:
                return await b.say("模型", f"{s.label} 用的是 {report.model_name(MODELS.get(s.model or '', s.model or '')) or '默认（' + str(user_setting('model')) + '）'}。")
            if cmd.name not in MODELS and cmd.name != "默认":
                return await b.say("模型", "可选：" + "、".join(MODELS) + "、默认")
            if engines.get(s.engine).pick_model != "menu":   # 别家（GPT、Kimi）的对话：中途不换成 Claude 的型号
                return await b.say("模型", f"{s.label} 用的是 {engines.get(s.engine).label}，中途换不了模型。")
            s.model = None if cmd.name == "默认" else MODELS[cmd.name]
            shown = report.model_name(s.model) if s.model else "默认"
        else:
            if not cmd.name:
                cur = s.effort or b.runner.default_effort
                return await b.say("深度", f"{s.label} 的思考深度：{report.EFFORT_CN.get(cur, cur) or '默认'}。")
            if cmd.name not in EFFORTS:
                return await b.say("深度", "可选：" + "、".join(EFFORTS))
            s.effort = EFFORTS[cmd.name]
            shown = cmd.name
        b.registry.save()
        await b.say("已设置", f"{s.label} 从下一句起用：{shown}。")

    async def _quota(self) -> None:
        b = self.b
        await b.runner.refresh_usage(force=True)
        if not b.runner.limits:
            return await b.say("额度", "这次没查到额度，稍后再试。")
        lines = []
        for key, (util, resets) in b.runner.limits.items():
            when = f"，{resets} 重置" if resets else ""
            lines.append(f"- {report.LIMIT_CN.get(key, key)}：已用 {util * 100:.0f}%{when}")
        await b.say("额度", "\n".join(lines))

    async def _mkdir(self, spec: str, text: str, inc: Incoming) -> None:
        b = self.b
        parent_tok, name = folders.split_new_path(spec)
        if not name or "/" in name or name.startswith("."):
            return await b.say("新建文件夹", "要写成“新建 位置/名字：要做的事”，比如“新建 personal/菜谱整理：…”。")
        if parent_tok:
            cands = folders.candidates(parent_tok, b.cfg)
            if len(cands) != 1:
                return await b.say("新建文件夹", f"位置“{parent_tok}”{'没找到' if not cands else '有好几个，请写完整路径或短名'}。")
            parent = cands[0]
        else:
            parent = Path(b.cfg.scratch_root).expanduser()
        target = parent / name
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        num = b.registry.reserve_num()
        await b.start_session(num, target, [], text, inc, note="（文件夹本来就有，直接用了）" if existed else "（已新建文件夹）")

    # ---------- 回编号 ----------
    async def ask_folder(self, ambiguous: str, cands: list[Path], **pending) -> None:
        """“新开 …”里的文件夹有好几个对得上：列出来等你回编号。"""
        self.pending = {"kind": "folder", "cands": cands, **pending}
        lines = [f"{i}. {folders.display(p)}" for i, p in enumerate(cands, 1)]
        await self.b.say("选文件夹", f"“{ambiguous}”找到几个，回编号选一个：\n\n" + "\n".join(lines))

    async def ask_route(self, cmd: Cmd, inc: Incoming, folder: str | None) -> None:
        """分派员拿不准：问你接着当前对话还是新开。"""
        b = self.b
        cur = b.registry.get(b.registry.current)
        where = f"在 {folder} 里" if folder and folders.candidates(folder, b.cfg) else "不指定文件夹"
        if not cur:
            return await b.new_session([folder] if where != "不指定文件夹" else [], cmd.text, inc)
        self.pending = {"kind": "route", "cmd": cmd, "inc": inc, "cur": cur.num,
                        "folder": folder if where != "不指定文件夹" else None}
        await b.say("接着还是新开", f"这句要接着 {cur.label}，还是新开一个？回编号：\n\n1. 接着 {cur.label}\n2. 新开（{where}）")

    async def pick(self, n: int | None) -> None:
        b = self.b
        p, self.pending = self.pending, None
        if p["kind"] == "folder":
            if not n or not 1 <= n <= len(p["cands"]):
                return await b.say("选文件夹", "编号不对，这次先取消了，重新发一次“新开 …”吧。")
            await b.new_session(p["tokens"], p["text"], p["inc"], resolved=p["resolved"] + [p["cands"][n - 1]],
                                note=p.get("note", ""), kind=p.get("task_kind"))
        elif p["kind"] == "desktop":
            await self._desktop(n, p["infos"], replace=p["num"])
        elif p["kind"] == "recent":
            await self._recent(p["folder"], pick=n)
        elif p["kind"] == "route":
            if n == 1:
                await b.message(Cmd("message", num=p["cur"], text=p["cmd"].text), p["inc"], routed=True)
            elif n == 2:
                await b.new_session([p["folder"]] if p["folder"] else [], p["cmd"].text, p["inc"])
            else:
                await b.say("接着还是新开", "编号不对，这句先没交出去，请重新发一次。")

    async def _recent(self, token: str, pick: int | None = None) -> None:
        b = self.b
        cands = folders.candidates(token, b.cfg)
        if len(cands) != 1:
            return await b.say("最近的对话", f"文件夹“{token}”{'没找到' if not cands else '有好几个，请写短名或完整路径'}。")
        d = cands[0]
        infos = await asyncio.to_thread(list_sessions, str(d), 10, 0, False)
        if not infos:
            return await b.say("最近的对话", f"{folders.display(d)} 里还没有对话。")
        if pick is None:
            lines = []
            for i, x in enumerate(infos, 1):
                when = datetime.fromtimestamp(x.last_modified / 1000 if x.last_modified > 1e12 else x.last_modified)
                title = (x.custom_title or x.summary or x.first_prompt or "").replace("\n", " ")[:40]
                lines.append(f"{i}. {when:%m-%d %H:%M}　{title}")
            self.pending = {"kind": "recent", "folder": token}
            return await b.say("最近的对话", f"{folders.display(d)} 最近的对话，回编号接着聊：\n\n" + "\n".join(lines))
        if not 1 <= pick <= len(infos):
            return await b.say("最近的对话", "编号不对。")
        x = infos[pick - 1]
        num = b.registry.reserve_num()
        name = report.brief(x.custom_title or x.summary or x.first_prompt or "接着聊", 16)
        s = b.registry.add(Session(num=num, name=name, cwd=str(d), claude_id=x.session_id, from_desktop=True))
        await b.say("接上了", f"{s.label} 接上了，之后直接说的话交给它。")

    async def _desktop(self, pick: int | None, infos: list | None = None, replace: int | None = None) -> None:
        """接上你最近在桌面端聊的那段：不用记文件夹和编号。远窗自己新开的、临时文件夹里的不算。"""
        b = self.b
        if infos is None:
            mine = {s.claude_id for s in b.registry.sessions.values() if not s.from_desktop}
            skip = (str(Path.home()), str(Path(b.cfg.scratch_root).expanduser()))
            everything = await asyncio.to_thread(list_sessions, None, 150, 0, False)
            infos = [x for x in everything
                     if x.session_id not in mine and x.cwd and x.cwd != skip[0] and not x.cwd.startswith(skip[1])][:5]
        if not infos:
            return await b.say("接着桌面", "没找到桌面端最近的对话。")
        pick = pick or 1
        if not 1 <= pick <= len(infos):
            return await b.say("接着桌面", f"编号不对，只有 1—{len(infos)}。")
        if replace and (old := b.registry.get(replace)) and old.status == "idle" and not old.hidden:
            old.hidden = True  # 刚才接错的那段收起来
        x = infos[pick - 1]
        title = (x.custom_title or x.summary or x.first_prompt or "接着聊").replace("\n", " ")
        s = next((v for v in b.registry.sessions.values() if v.claude_id == x.session_id), None)
        new = s is None
        if s:
            s.hidden = False
            b.registry.current = s.num
            b.registry.save()
        else:
            s = b.registry.add(Session(num=b.registry.reserve_num(), name=report.brief(title, 16),
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
        await b.say("接着桌面", "\n\n".join(lines))

    # ---------- 长连接 ----------
    def client(self):
        """钉钉 Stream 长连接（只在开了钉钉时才导入 dingtalk_stream）。"""
        import dingtalk_stream
        chat, cards = self, self.b.dcards

        class Handler(dingtalk_stream.CallbackHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                asyncio.create_task(chat.on_raw(callback.data))
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        class CardHandler(dingtalk_stream.CallbackHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                asyncio.create_task(cards.on_click(callback.data))
                return dingtalk_stream.AckMessage.STATUS_OK, {}

        client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(self.b.cfg.client_id, self.b.dt.secret))
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
        client.register_callback_handler(dingtalk_stream.Card_Callback_Router_Topic, CardHandler())
        return client
