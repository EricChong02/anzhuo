"""事件总线（events.py）和额度变档事件（Bridge._quota_check）。"""
import asyncio
import time
from types import SimpleNamespace as NS

from remote_claude.app import Bridge, quota_level
from remote_claude.config import Config
from remote_claude.events import Bus
from remote_claude.sessions import Session


def test_bus_order_errors_and_claim():
    async def go():
        bus, seen = Bus(), []
        bus.on("x", lambda v: seen.append(("late", v)), order=90)
        bus.on("x", lambda v: 1 / 0, order=20)                  # 坏的订阅者不拖累别的

        async def early(v):
            seen.append(("early", v))
        bus.on("x", early, order=10)
        await bus.emit("x", 1)
        assert seen == [("early", 1), ("late", 1)]
        bus.on("c", lambda: False)
        bus.on("c", lambda: True)
        bus.on("c", lambda: seen.append("不该叫到"))
        assert await bus.claim("c") and "不该叫到" not in seen
        assert not await bus.claim("没人订")
    asyncio.run(go())


def test_quota_level():
    assert quota_level({}) == "充足"
    assert quota_level({"five_hour": (0.5, "x"), "seven_day": (0.8, None)}) == "偏紧"
    assert quota_level({"five_hour": (0.95, "x")}) == "告急"
    assert quota_level({"five_hour": None, "x": "坏的"}) == "充足"


def test_quota_events(tmp_path):
    """额度用多了：充足 → 偏紧 → 告急；撞线后等撞线的对话到点，才回到按用量分档（发一次“恢复”）。"""
    async def go():
        b = Bridge(Config(client_id="t", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s")), NS(secret="x"), tmp_path / "d")
        got = []
        b.events.on("quota", lambda **k: got.append((k["engine"], k["prev"], k["level"])))
        b.runner.limits.update({"five_hour": (0.8, "x")})
        await b._quota_check()
        await b._quota_check()                                   # 没变档不重复发
        b.runner.limits.update({"five_hour": (0.92, "x")})
        await b._quota_check()
        s = b.registry.add(Session(num=b.registry.reserve_num(), name="a", cwd=str(tmp_path), limit_until=time.time() + 600))
        await b._quota_level("claude", "撞线", num=s.num)
        b.runner.limits.update({"five_hour": (0.1, "x")})
        await b._quota_check()                                   # 还没到点：仍算撞线
        s.limit_until = time.time() - 1
        await b._quota_check()
        assert [g for g in got if g[0] == "claude"] == [("claude", "充足", "偏紧"), ("claude", "偏紧", "告急"),
                                                        ("claude", "告急", "撞线"), ("claude", "撞线", "充足")]
    asyncio.run(go())
