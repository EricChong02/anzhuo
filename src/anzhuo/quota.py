"""额度调度：每家额度按“用了多少 + 用得多快”分档（充足 / 偏紧 / 告急；撞线由 Bridge 另记），新开对话选引擎时参考。

读数来自各家引擎的 limits()：{"five_hour": (已用比例, "11月8日 14:00"), "seven_day": (...)}。
速度 = 已用 ÷ 窗口已过的比例（窗口开头读数不稳，已过比例至少按 20% 算）；按这速度到重置时会用到 速度 那么多。
- 告急：已用 ≥ 90%，或已用过半且按这速度到重置时要用到 150%（远远不够）；
- 偏紧：已用 ≥ 75%，或已用 ≥ 25% 且按这速度到重置前就会用完；
- 其余充足。重置时间读不出来时只看已用多少。
"""
from __future__ import annotations

import time

from .usage import reset_epoch

WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
NAMES = {"five_hour": "5 小时", "seven_day": "本周"}
LEVELS = ("充足", "偏紧", "告急", "撞线")   # 从好到坏
TIGHT = ("偏紧", "告急", "撞线")
SWITCH = ("告急", "撞线")   # 到这两档新开的活才换备选；偏紧只提示（质量优先，不为省额度降质量）
FULL_AT, URGENT_AT = 0.75, 0.9   # 只看已用多少时的门槛
MIN_ELAPSED = 0.2
WEEKDAYS = "一二三四五六日"


def rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def _util(v) -> float | None:
    return float(v[0]) if isinstance(v, (list, tuple)) and v and isinstance(v[0], (int, float)) else None


def window(key: str, v, now: float | None = None) -> dict | None:
    """一个窗口的情况：{used, reset, pace, eta, level}；pace = 按这速度到重置时会用到多少，eta = 按这速度何时用完（重置前用不完是 None）。"""
    used = _util(v)
    if used is None:
        return None
    now = now or time.time()
    span = WINDOWS.get(key)
    reset = reset_epoch(v[1] if len(v) > 1 else None, now)
    pace = eta = None
    if span and reset and now < reset <= now + span:
        elapsed = span - (reset - now)
        pace = used / max(elapsed / span, MIN_ELAPSED)
        if 0 < used < 1 and elapsed > 0:
            at = now + (1 - used) / (used / elapsed)
            eta = at if at < reset else None
    if used >= URGENT_AT or (pace is not None and used >= 0.5 and pace >= 1.5):
        level = "告急"
    elif used >= FULL_AT or (pace is not None and used >= 0.25 and pace >= 1.0):
        level = "偏紧"
    else:
        level = "充足"
    return {"used": used, "reset": reset, "pace": pace, "eta": eta, "level": level}


def forecast(limits: dict, now: float | None = None) -> dict[str, dict]:
    return {k: w for k, v in (limits or {}).items() if (w := window(k, v, now))}


def level(limits: dict, now: float | None = None) -> str:
    """额度在哪一档：看最紧的那个窗口。"""
    return max((w["level"] for w in forecast(limits, now).values()), key=rank, default="充足")


def when(t: float, now: float | None = None) -> str:
    """时刻说成人话：今天 15:20 / 明天 9:00 / 周四 9:00。"""
    now = now or time.time()
    a, b = time.localtime(now), time.localtime(t)
    days = (time.mktime((b.tm_year, b.tm_mon, b.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((a.tm_year, a.tm_mon, a.tm_mday, 0, 0, 0, 0, 0, -1))) // 86400
    day = {0: "今天", 1: "明天"}.get(int(days), f"周{WEEKDAYS[b.tm_wday]}")
    return f"{day} {b.tm_hour}:{b.tm_min:02d}"


def describe(limits: dict, now: float | None = None) -> str:
    """一句话说清最紧的窗口：本周 62%，按这速度周四 9:00 用完。没读数返回空。"""
    ws = forecast(limits, now)
    if not ws:
        return ""
    k, w = max(ws.items(), key=lambda kv: (rank(kv[1]["level"]), kv[1]["used"]))
    out = f"{NAMES.get(k, k)} {w['used'] * 100:.0f}%"
    return out + (f"，按这速度{when(w['eta'], now)} 用完" if w["eta"] else "")
