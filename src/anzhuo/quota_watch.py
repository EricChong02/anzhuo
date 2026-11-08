"""额度监视：定时读各家额度、分档（充足 / 偏紧 / 告急 / 撞线），变档时发 quota 事件；撞线的对话到点后替你说“继续”。
算法（速度、分档线）在 quota.py；这里只管什么时候读、记下现在在哪一档、到点接着做。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from . import engines, quota
from .dingtalk import Incoming

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger(__name__)
READ_EVERY = 600   # 多久读一次额度（秒）


class QuotaWatch:
    def __init__(self, bridge: Bridge):
        self.b = bridge
        self.levels: dict[str, str] = {}   # 各家额度现在在哪一档
        bridge.events.on("quota", self._said, order=90)

    async def set_level(self, engine: str, level: str, num: int | None = None) -> None:
        """记下某家额度到了哪一档；变了才发事件。"""
        prev = self.levels.get(engine, "充足")
        if level == prev:
            return
        self.levels[engine] = level
        await self.b.events.emit("quota", engine=engine, level=level, prev=prev,
                                 limits=dict(engines.get(engine).limits(self.b.runner)), num=num)

    async def check(self) -> None:
        """刚读过额度：各家按用了多少分档。撞线的要等撞线的对话都到点恢复了，才按用量重新分档。"""
        now = time.time()
        for eng in engines.known():
            if not eng.has_quota:
                continue
            if self.levels.get(eng.key) == "撞线" and any(
                    s.limit_until > now and engines.get(s.engine) is eng for s in self.b.registry.visible()):
                continue
            await self.set_level(eng.key, quota.level(eng.limits(self.b.runner)))

    def _said(self, engine: str, level: str, prev: str, limits: dict, num: int | None = None) -> None:
        e = engines.get(engine)
        log.info("%s 额度：%s → %s（%s）", e.label, prev, level, quota.describe(limits) or "没读数")

    async def hit_limit(self, s, until: float) -> None:
        """这个对话撞上了额度：记下几点恢复，到点自动接着做，这家记成撞线。"""
        s.limit_until = until
        asyncio.create_task(self.auto_resume(s.num, until))
        await self.set_level(s.engine, "撞线", num=s.num)

    async def auto_resume(self, num: int, until: float) -> None:
        """额度恢复后一分钟，替你说一句“继续”（你点了“别自动接”、或已经自己接着说了就不说）。"""
        b = self.b
        await asyncio.sleep(max(0.0, until + 60 - time.time()))
        s = b.registry.get(num)
        if s:
            try:
                await engines.get(s.engine).refresh_quota(b.runner, force=True)
            except Exception:  # noqa: BLE001
                log.debug("读额度失败", exc_info=True)
            await self.check()   # 撞线的那家：到点了就按用量回到 充足 / 偏紧（发 quota 事件）
        if not s or s.limit_until != until or not s.auto_resume or b.runner.busy(num) or s.hidden or s.relay_to:
            return
        s.limit_until = 0.0
        b.record("远窗", "额度恢复，自动接着做", num=num)
        inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text="继续", via="auto")
        await b.submit(s, "继续", inc)

    def resume_waiting(self) -> None:
        """重启前撞了额度在等恢复的，接着等。"""
        for s in self.b.registry.sessions.values():
            if s.limit_until and s.auto_resume:
                asyncio.create_task(self.auto_resume(s.num, s.limit_until))

    async def loop(self) -> None:
        """Claude 的额度每 10 分钟读一次（重启后不用等第一轮做完，侧栏一打开就有）；有别家对话时别家的也读。"""
        b = self.b
        while True:
            try:
                await b.runner.refresh_usage(force=True)
                if week := b.runner.limits.get("seven_day"):
                    b.spend.snap(round(week[0] * 100, 1))   # 刚读到的本周额度 %，换算比例用
            except Exception:  # noqa: BLE001
                log.debug("读 Claude 额度失败", exc_info=True)
            for eng in {engines.get(s.engine) for s in b.registry.visible()} - {engines.CLAUDE}:   # 别家：有对话在用、能查才查
                if not eng.has_quota:
                    continue
                try:
                    await eng.refresh_quota(b.runner, force=True)
                except Exception:  # noqa: BLE001
                    log.debug("读 %s 额度失败", eng.label, exc_info=True)
            await self.check()
            await asyncio.sleep(READ_EVERY)
