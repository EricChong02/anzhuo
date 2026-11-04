"""额度消耗记录：每轮做完记一行（usage.jsonl），管理页按天、按对话、按账号汇总。

订阅不按 token 计费，官方也不给“这一轮用了周额度百分之几”。所以：
- 每轮记 Claude 报的“按 API 标价折算的金额”（美元）：它已经按模型（Opus 比 Sonnet 贵）、缓存读写的折扣加权，
  比原始 token 数更接近额度的真实消耗；原始 token 也记着，明细里看。
- 同时记下当时的 5 小时、本周额度百分比，管理页用它看额度走势。
- Codex（ChatGPT 会员）只有 token，没有金额，单独列。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DAY = 86400


def _tokens(models: dict) -> dict:
    out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for m in models.values():
        out["input"] += int(m.get("inputTokens") or 0)
        out["output"] += int(m.get("outputTokens") or 0)
        out["cache_read"] += int(m.get("cacheReadInputTokens") or 0)
        out["cache_write"] += int(m.get("cacheCreationInputTokens") or 0)
    return out


def row(s, turn, limits: dict, now: float | None = None) -> dict | None:
    """一轮的消耗记录；这一轮没有消耗数据（比如没连上）返回 None。"""
    u = turn.usage or {}
    base = {"t": round(now or turn.ended or time.time(), 1), "owner": s.owner, "num": s.num, "name": s.name,
            "engine": s.engine, "request": (turn.request or "")[:60]}
    if "codex" in u:
        total = int(u["codex"].get("total") or 0)
        used = max(0, total - (s.codex_tokens or 0))   # Codex 报的是会话累计
        s.codex_tokens = total
        if not used:
            return None
        return {**base, "model": turn.model or "gpt", "cost": 0.0, "tokens": {"total": used}}
    models = u.get("models") or {}
    if not models and not u.get("cost"):
        return None
    tok = _tokens(models)
    pct = {k: round(v[0] * 100, 1) for k, v in (limits or {}).items() if k in ("five_hour", "seven_day")}
    return {**base, "model": ", ".join(models) or turn.model, "cost": round(float(u.get("cost") or 0), 4),
            "tokens": {**tok, "total": sum(tok.values())}, "limits": pct}


def append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load(path: Path, since: float = 0) -> list[dict]:
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get("t", 0) >= since:
            out.append(r)
    return out


def summary(rows: list[dict], names: dict[int, str], days: int = 30, now: float | None = None) -> dict:
    """管理页要的数：每天（金额、token、轮数、当天最后的本周额度 %）、每个对话、每个账号。"""
    now = now or time.time()
    lt = time.localtime(now)
    today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    start = today0 - (days - 1) * DAY
    daily = []
    for i in range(days):
        d0 = start + i * DAY
        daily.append({"day": time.strftime("%m-%d", time.localtime(d0)), "cost": 0.0, "tokens": 0, "turns": 0,
                      "gpt_tokens": 0, "week_pct": None, "by_owner": {}})
    convs: dict[int, dict] = {}
    owners: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("t", 0)):
        t = r.get("t", 0)
        if t < start:
            continue
        i = min(days - 1, int((t - start) // DAY))
        d = daily[i]
        cost, tok = float(r.get("cost") or 0), int((r.get("tokens") or {}).get("total") or 0)
        gpt = r.get("engine") == "codex"
        d["turns"] += 1
        if gpt:
            d["gpt_tokens"] += tok
        else:
            d["cost"] += cost
            d["tokens"] += tok
            d["by_owner"][r.get("owner", "admin")] = d["by_owner"].get(r.get("owner", "admin"), 0) + cost
        if (r.get("limits") or {}).get("seven_day") is not None:
            d["week_pct"] = r["limits"]["seven_day"]
        c = convs.setdefault(r["num"], {"num": r["num"], "name": names.get(r["num"]) or r.get("name") or f"#{r['num']}",
                                        "owner": r.get("owner", "admin"), "engine": r.get("engine", "claude"),
                                        "cost": 0.0, "tokens": 0, "turns": 0, "last": 0, "models": set()})
        c["cost"] += cost
        c["tokens"] += tok
        c["turns"] += 1
        c["last"] = max(c["last"], t)
        c["models"].update(m.strip() for m in str(r.get("model") or "").split(",") if m.strip())
        o = owners.setdefault(r.get("owner", "admin"), {"owner": r.get("owner", "admin"), "cost": 0.0, "tokens": 0,
                                                         "gpt_tokens": 0, "turns": 0})
        o["turns"] += 1
        if gpt:
            o["gpt_tokens"] += tok
        else:
            o["cost"] += cost
            o["tokens"] += tok
    total = sum(c["cost"] for c in convs.values()) or 0.0
    conv_rows = sorted(convs.values(), key=lambda c: (-c["cost"], -c["tokens"]))
    for c in conv_rows:
        c["models"] = sorted(c["models"])
        c["share"] = round(100 * c["cost"] / total, 1) if total else 0.0
        c["cost"] = round(c["cost"], 4)
    for d in daily:
        d["cost"] = round(d["cost"], 4)
        d["by_owner"] = {k: round(v, 4) for k, v in d["by_owner"].items()}
    return {"days": daily, "conversations": conv_rows[:100], "owners": sorted(owners.values(), key=lambda o: o["owner"]),
            "total_cost": round(total, 4), "since": start}


# ---------- 受限账号的周额度 ----------
WEEK = 7 * DAY
DEFAULT_SHARE = 0.2   # 受限账号默认可用主账号周额度的 1/5（2026-11-04 庄毅定）


def reset_epoch(text: str | None, now: float | None = None) -> float | None:
    """“11月6日 10:00”这种重置时间 → 时间戳（年份取离现在最近的那个）。"""
    import re
    from datetime import datetime
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日 (\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    now = now or time.time()
    year = time.localtime(now).tm_year
    cands = []
    for y in (year - 1, year, year + 1):
        try:
            cands.append(datetime(y, *map(int, m.groups())).timestamp())
        except ValueError:
            pass
    return min(cands, key=lambda t: abs(t - now)) if cands else None


def rate(rows: list[dict], start: float) -> float | None:
    """这一周里“每 1 美元（标价折算）约等于周额度的百分之几”。
    用远窗每轮做完时记下的周额度 % 和金额推算：第一条到最后一条，周 % 涨了多少 ÷ 这期间花了多少。
    你在别处（claude.ai、电脑上的 Claude Code）用的也算在周 % 里，所以推出来偏大，换算受限账号的用量会偏保守。
    数据太少（不到 2 美元、周 % 没涨过 1）返回 None。"""
    rs = [r for r in rows if r.get("t", 0) >= start and r.get("engine") != "codex"
          and (r.get("limits") or {}).get("seven_day") is not None]
    if len(rs) >= 2:
        dp = rs[-1]["limits"]["seven_day"] - rs[0]["limits"]["seven_day"]
        dc = sum(float(r.get("cost") or 0) for r in rs[1:])
        if dc >= 2 and dp >= 1:
            return dp / dc
    if rs:
        cost = sum(float(r.get("cost") or 0) for r in rows if r.get("t", 0) >= start and r.get("engine") != "codex")
        p = rs[-1]["limits"]["seven_day"]
        if cost >= 2 and p >= 1:
            return p / cost
    return None


def quota(rows: list[dict], owner: str, share: float, week_reset: float | None, saved_rate: float | None,
          now: float | None = None) -> dict:
    """受限账号本周用了自己额度的百分之几。额度 = 主账号周额度 × share，和主账号同一时间重置。
    pct 是相对它自己的额度（100 = 用完）；used 是折算成主账号周额度的百分比。"""
    now = now or time.time()
    reset = week_reset if week_reset and week_reset > now else None
    start = (reset - WEEK) if reset else now - WEEK
    r = rate(rows, start) or saved_rate
    cost = sum(float(x.get("cost") or 0) for x in rows
               if x.get("t", 0) >= start and x.get("owner") == owner and x.get("engine") != "codex")
    used = cost * r if r else 0.0
    cap = share * 100
    return {"share": share, "cap": round(cap, 1), "cost": round(cost, 4), "used": round(used, 2),
            "pct": round(min(999.0, 100 * used / cap), 1) if cap else 100.0, "rate": r, "calibrating": r is None,
            "reset_at": reset, "start": start}
