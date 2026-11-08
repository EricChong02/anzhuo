"""钉钉里的卡片：工作台（在做的对话一张卡，跟着挪到聊天最下面）、危险操作确认卡、“要你定”选择卡、直接回答的回复卡，
以及卡片按钮被点之后怎么办。卡片长什么样在 cards.py；这里管发、改、记住哪张卡属于哪个对话（重启后按钮还能用）。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import cards
from .commands import Cmd
from .dingtalk import Incoming

if TYPE_CHECKING:
    from .app import Bridge
    from .report import Turn
    from .sessions import Session

log = logging.getLogger(__name__)
DINGTALK_FILE_MAX = 20 * 1024 * 1024  # 钉钉单个文件上限
KEEP = 100   # 只记最近这么多张卡


class DingCards:
    def __init__(self, bridge: Bridge):
        self.b = bridge
        self.path = bridge.data_dir / "cards.json"
        self.store: dict[str, dict] = self._load()   # 卡片 → 它属于哪个对话、是什么卡
        self.board: str | None = None  # 工作台卡：所有在做的对话一张卡，始终挪到聊天最下面
        self.board_task: asyncio.Task | None = None
        self.board_lock = asyncio.Lock()
        self.board_shown = ""
        self.progress_every = (15.0, 45.0)  # 工作台多久更新一次：都在前 5 分钟 / 有做得久的（省钉钉调用次数）

    @property
    def dt(self):
        return self.b.dt

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.store = dict(list(self.store.items())[-KEEP:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.store, ensure_ascii=False))

    # ---------- 工作台 ----------
    def _active(self) -> list[tuple[str, int, Turn | None]]:
        """在做的和排队的对话，给工作台用。"""
        b, items = self.b, []
        for num, s in sorted(b.registry.sessions.items()):
            if s.owner != "admin":
                continue
            if num in b.runner.turns:
                items.append((s.label, num, b.runner.turns[num]))
            elif b.runner.busy(num):
                items.append((s.label, num, None))
        return items

    async def post_board(self) -> None:
        """在聊天最下面发一张新的工作台，旧的那张缩成一行“挪到下面了”。"""
        async with self.board_lock:
            old, oid = self.board, f"b-{int(time.time() * 1000)}"
            data = cards.board_data(self._active())
            await self.dt.create_card(self.b.user_id, oid, data)
            self.board, self.board_shown = oid, data["markdown"]
            self.store[oid] = {"kind": "board", "num": 0}
            self._save()
            if old:
                try:
                    await self.dt.update_card(old, cards.board_data([], moved=True))
                except Exception:  # noqa: BLE001
                    log.debug("旧工作台收起失败", exc_info=True)
        if not self.board_task or self.board_task.done():
            self.board_task = asyncio.create_task(self._board_loop())

    async def follow(self) -> None:
        """远窗刚发了别的消息：还有活在做，就把工作台挪到它下面，免得被顶上去看不到；都做完了就当场收尾。"""
        if not self.board:
            return
        try:
            if self._active():
                await self.post_board()
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

    # ---------- 各种卡 ----------
    async def reply(self, answer: str, text: str) -> None:
        """远窗直接回答；有当前对话时附一个“交给 #N”按钮，判断错了点一下就转过去。"""
        b = self.b
        cur = b.registry.get(b.registry.current)
        if not cur or cur not in b.registry.visible():
            return await b.say("回复", f"{answer}\n\n---\n\n远窗直接回的（Haiku），没占用任何对话")
        oid = f"r{cur.num}-{int(time.time() * 1000)}"
        try:
            await self.dt.create_card(b.user_id, oid, cards.reply_data(answer, cur.label))
        except Exception:  # noqa: BLE001
            log.warning("发回复卡失败", exc_info=True)
            return await b.say("回复", f"{answer}\n\n---\n\n远窗直接回的（Haiku）。要说给 {cur.label}，发“#{cur.num} 原话”")
        b.record("远窗", answer, title="回复卡", forward_to=cur.num)
        self.store[oid] = {"kind": "reply", "num": cur.num, "text": text, "answer": answer}
        self._save()
        await self.follow()

    async def ask_confirm(self, s: Session, c: dict) -> None:
        """危险操作等你点头：发一张允许 / 不允许的按钮卡。"""
        try:
            oid = f"c{s.num}-{int(time.time() * 1000)}"
            await self.dt.create_card(self.b.user_id, oid, cards.confirm_data(s.label, c["what"], c["cmd"], s.num))
            self.store[oid] = {"kind": "confirm", "num": s.num, "what": c["what"], "cmd": c["cmd"]}
            self._save()
        except Exception:  # noqa: BLE001 - 钉钉发不出去，网页和手机通知照样能点
            log.exception("发确认卡失败")

    async def confirm_done(self, num: int, result: str) -> None:
        """不管在哪点的，把这个对话还没答的确认卡改成结果。"""
        for oid, info in self.store.items():
            if info.get("kind") == "confirm" and info["num"] == num and not info.get("done"):
                info["done"] = True
                s = self.b.registry.get(num)
                try:
                    await self.dt.update_card(oid, cards.confirm_data(s.label if s else f"#{num}", info["what"],
                                                                      info["cmd"], num, result))
                except Exception:  # noqa: BLE001
                    log.exception("改确认卡失败")
        self._save()

    async def deliver(self, s: Session, turn: Turn, title: str, body: str) -> None:
        """把一轮的完整结果发回钉钉：报告、图片、文件；末尾有“要你定”就附一张选择卡。"""
        b = self.b
        try:
            await b.say(title, body, board=False)
            for img in turn.images:
                await self.dt.send_image(b.user_id, Path(img))
            for f in turn.files:
                path = Path(f)
                if path.stat().st_size > DINGTALK_FILE_MAX:
                    await b.say("文件太大", f"{path.name} 超过 20MB，钉钉发不了，到网页对话里下载。", board=False)
                    continue
                try:
                    await self.dt.send_file(b.user_id, path)
                except Exception:  # noqa: BLE001
                    log.exception("发文件失败")
                    await b.say("文件没发出去", f"{path.name} 钉钉没发出去，到网页对话里下载。", board=False)
            if not turn.is_error and (d := cards.parse_decision(turn.final_text)):
                oid = f"d{s.num}-{int(time.time() * 1000)}"
                await self.dt.create_card(b.user_id, oid, cards.decision_data(s.label, d))
                self.store[oid] = {"kind": "decision", "num": s.num, "question": d.question,
                                   "options": d.options, "recommend": d.recommend}
                self._save()
        except Exception:
            log.exception("推送 #%s 的结果失败", s.num)
        await self.follow()

    # ---------- 按钮被点 ----------
    async def on_click(self, data: dict) -> None:
        """卡片上的按钮被点了：停下，或把选择交回那个对话。"""
        b = self.b
        if data.get("userId") not in b.cfg.allowed_user_ids:
            log.warning("忽略一次卡片点击：%s", data.get("userId"))
            return
        oid, action = data.get("outTrackId", ""), cards.action_of(data)
        info = self.store.get(oid)
        log.info("卡片点击：%s %s", oid, action)
        b.record("你", f"点了卡片按钮 {action}", card=oid, raw=json.dumps(data, ensure_ascii=False)[:1500])
        if not info:
            return await b.say("按钮失效了", "这张卡片的按钮已经失效，直接回字告诉我吧。")
        if action.startswith(("allow:", "deny:")):
            num = int(action.split(":")[1])
            if not await b.answer_confirm(num, action.startswith("allow"), "钉钉"):
                await b.say("确认", f"#{num} 这一步已经过去了（点过了或超时了）。")
            return
        if action.startswith("stop:"):
            num = int(action.split(":")[1])
            if not await b.runner.stop(num):
                await b.say("停", f"#{num} 已经做完了，不用停。")
            return
        if not action:
            return await b.say("按钮没认出来", "这个按钮我没认出来（已记下来排查），先直接回字告诉我选哪个吧。")
        s = b.registry.get(info["num"])
        if not s:
            return await b.say("没有这个对话", f"#{info['num']} 已经不在了。")
        if action == "stop":
            if not await b.runner.stop(s.num):
                await b.say("停", f"{s.label} 已经做完了，不用停。")
            return
        if action == "fwd" and info["kind"] == "reply":
            if info.get("done"):
                return
            info["done"] = True
            self._save()
            b.chat = [c for c in b.chat if c[1] != info["text"]]  # 这句不算远窗的闲聊了
            await self.dt.update_card(oid, cards.reply_data(info["answer"], s.label, forwarded=True))
            inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=info["text"])
            await b.message(Cmd("message", num=s.num, text=info["text"]), inc, routed=True)
            return
        if action.startswith("pick:") and info["kind"] == "decision":
            if info.get("picked"):
                return
            letter = action.split(":", 1)[1]
            options = dict(info["options"])
            info["picked"] = letter
            self._save()
            d = cards.Decision(info["question"], [tuple(o) for o in info["options"]], info.get("recommend"))
            await self.dt.update_card(oid, cards.decision_data(s.label, d, picked=letter))
            text = f"选 {letter}：{options.get(letter, '')}"
            inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=text)
            await b.message(Cmd("message", num=s.num, text=text), inc, routed=True)
