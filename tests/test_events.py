"""事件总线（events.py）和额度变档事件（QuotaWatch.check）。"""
import asyncio
import time

import pytest
from types import SimpleNamespace as NS

from anzhuo import quota, superagent
from anzhuo.app import Bridge
from anzhuo.config import Config
from anzhuo.events import Bus
from anzhuo.sessions import Session


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
    """重置时间读不出来：只看已用多少。"""
    assert quota.level({}) == "充足"
    assert quota.level({"five_hour": (0.5, "x"), "seven_day": (0.8, None)}) == "偏紧"
    assert quota.level({"five_hour": (0.95, "x")}) == "告急"
    assert quota.level({"five_hour": None, "x": "坏的"}) == "充足"


def _at(t: float) -> str:
    lt = time.localtime(t)
    return f"{lt.tm_mon}月{lt.tm_mday}日 {lt.tm_hour:02d}:{lt.tm_min:02d}"


def test_quota_pace_and_eta():
    """按消耗速度分档：同样用了 40%，周初是告急前兆（偏紧），周末就充足；还给出按这速度何时用完。"""
    now = time.mktime((2026, 11, 8, 12, 0, 0, 0, 0, -1))
    day = 86400
    early = {"seven_day": (0.4, _at(now + 6 * day))}          # 过了 1 天用了 40%：按这速度到重置要用到 200%
    w = quota.forecast(early, now)["seven_day"]
    assert w["level"] == "偏紧" and w["pace"] == pytest.approx(2.0) and w["eta"] == pytest.approx(now + 1.5 * day, abs=60)
    assert quota.level({"seven_day": (0.6, _at(now + 6 * day))}, now) == "告急"     # 过半又远远不够
    assert quota.level({"seven_day": (0.4, _at(now + 1 * day))}, now) == "充足"     # 周末才用 40%
    assert quota.forecast({"seven_day": (0.4, _at(now + 1 * day))}, now)["seven_day"]["eta"] is None
    assert quota.level({"five_hour": (0.1, _at(now + 4.9 * 3600))}, now) == "充足"  # 窗口刚开头：已过比例至少按 20% 算，不慌
    assert quota.describe(early, now) == f"本周 40%，按这速度{quota.when(now + 1.5 * day, now)} 用完"
    assert quota.when(now + 3600, now) == "今天 13:00" and quota.when(now + day, now) == "明天 12:00"
    assert quota.describe({}) == ""


def test_quota_events(tmp_path):
    """额度用多了：充足 → 偏紧 → 告急；撞线后等撞线的对话到点，才回到按用量分档（发一次“恢复”）。"""
    async def go():
        b = Bridge(Config(client_id="t", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s")), NS(secret="x"), tmp_path / "d")
        got = []
        b.events.on("quota", lambda **k: got.append((k["engine"], k["prev"], k["level"])))
        b.runner.limits.update({"five_hour": (0.8, "x")})
        await b.quota.check()
        await b.quota.check()                                   # 没变档不重复发
        b.runner.limits.update({"five_hour": (0.92, "x")})
        await b.quota.check()
        s = b.registry.add(Session(num=b.registry.reserve_num(), name="a", cwd=str(tmp_path), limit_until=time.time() + 600))
        await b.quota.set_level("claude", "撞线", num=s.num)
        b.runner.limits.update({"five_hour": (0.1, "x")})
        await b.quota.check()                                   # 还没到点：仍算撞线
        s.limit_until = time.time() - 1
        await b.quota.check()
        assert [g for g in got if g[0] == "claude"] == [("claude", "充足", "偏紧"), ("claude", "偏紧", "告急"),
                                                        ("claude", "告急", "撞线"), ("claude", "撞线", "充足")]
    asyncio.run(go())


def test_relay_keeps_boss(tmp_path):
    """接力：总管派的活接力后照旧回报总管；总管自己接力，角色、没看的情况、手下的对话都交给新的。"""
    b = Bridge(Config(client_id="t", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), super_agent=True),
               NS(secret="x"), tmp_path / "d")
    boss = superagent.find(b)
    boss.notes = ["10:00 用户直接对 #3 说了话"]
    w = b.registry.add(Session(num=b.registry.reserve_num(), name="活", cwd=str(tmp_path), boss=boss.num))
    w2 = asyncio.run(b.relays.open(w, "交接"))
    assert w2.boss == boss.num and superagent.boss_of(b, w2) is boss
    nb = asyncio.run(b.relays.open(boss, "交接"))
    assert nb.role == superagent.ROUTER and boss.role == "" and not boss.pinned and nb.notes and not boss.notes
    assert superagent.find(b) is nb and w.boss == w2.boss == nb.num and b.registry.current == nb.num
