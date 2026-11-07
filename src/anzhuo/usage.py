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


KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def _minus(cur: dict, prev: dict) -> dict:
    """各模型累计 token 减去上一轮的累计；这一轮没用到的模型去掉。"""
    out = {}
    for m, v in cur.items():
        p = prev.get(m) or {}
        d = {k: max(0, int(v.get(k) or 0) - int(p.get(k) or 0)) for k in KEYS}
        if any(d.values()):
            out[m] = d
    return out


def row(s, turn, limits: dict, now: float | None = None, last: dict | None = None) -> dict | None:
    """一轮的消耗记录；这一轮没有消耗数据（比如没连上）返回 None。
    last：这个对话上一条记录的累计值（{"cost", "tok", "models"}），对话里还没存累计值时（小E 刚升级）拿它当起点。"""
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
    models, cost = u.get("models") or {}, float(u.get("cost") or 0)
    if not models and not cost:
        return None
    # Claude 报的金额、token 是整个会话累计的（接着上次的会话也接着累计）：这一轮 = 减去上一轮的累计。
    # 累计变小了说明换了新会话（分叉、重开），这一轮就是全部。
    cum = {"cost": round(cost, 4), "tok": _tokens(models), "models": models}
    prev = s.claude_usage or last or {}
    s.claude_usage = cum
    tok = cum["tok"]
    if prev and cost + 1e-6 >= float(prev.get("cost") or 0):
        cost -= float(prev.get("cost") or 0)
        if prev.get("models"):
            models = _minus(models, prev["models"])
            tok = _tokens(models)
        else:
            tok = {k: max(0, v - int((prev.get("tok") or {}).get(k) or 0)) for k, v in tok.items()}
        if cost < 0.00005 and not any(tok.values()):
            return None
    pct = {k: round(v[0] * 100, 1) for k, v in (limits or {}).items() if k in ("five_hour", "seven_day")}
    return {**base, "model": ", ".join(models) or turn.model, "cost": round(cost, 4),
            "tokens": {**tok, "total": sum(tok.values())}, "limits": pct, "cum": {"cost": cum["cost"], "tok": cum["tok"]}}


def append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def migrate(path: Path) -> int:
    """2026-11-04 之前记的 Claude 行是会话累计值（当时当成了每轮的）：按对话顺序减成每轮的，补上 cum。只改没有 cum 的行；返回改了几行。"""
    rows = load(path)
    prev: dict[int, dict] = {}
    n = 0
    for r in sorted(rows, key=lambda x: x.get("t", 0)):
        if r.get("engine") == "codex":
            continue
        if "cum" not in r:
            raw = {"cost": float(r.get("cost") or 0), "tok": {k: int((r.get("tokens") or {}).get(k) or 0)
                                                               for k in ("input", "output", "cache_read", "cache_write")}}
            p = prev.get(r.get("num"))
            if p and raw["cost"] + 1e-6 >= p["cost"]:
                tok = {k: max(0, v - p["tok"].get(k, 0)) for k, v in raw["tok"].items()}
                r["cost"] = round(raw["cost"] - p["cost"], 4)
                r["tokens"] = {**tok, "total": sum(tok.values())}
            r["cum"] = raw
            n += 1
        prev[r.get("num")] = r["cum"]
    if n:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        tmp.replace(path)
    return n


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


def relay_groups(sessions: dict) -> dict[int, int]:
    """接力链归到最初的对话；只沿同账号的真实关系，不按名称猜测。"""
    groups = {}
    for num in sessions:
        chain, current = [], num
        while current not in chain:
            chain.append(current)
            session = sessions[current]
            parent = session.relay_from
            if parent not in sessions or sessions[parent].owner != session.owner:
                break
            current = parent
        else:
            # 损坏的循环也要稳定归组，避免死循环或重复记账。
            current = min(chain[chain.index(current):])
        groups[num] = current
    return groups


def summary(rows: list[dict], names: dict[int, str], days: int = 30, now: float | None = None,
            rate: float | None = None, groups: dict[int, int] | None = None, since: float | None = None) -> dict:
    """管理页要的数：每天（金额、token、轮数、当天最后的本周额度 %）、每个对话、每个账号。
    rate：每 1 美元约等于主账号周额度的百分之几（见 rate()）；页面用它把金额换成“占周额度 %”。"""
    now = now or time.time()
    lt = time.localtime(now)
    today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    start = today0 - (days - 1) * DAY if since is None else since
    chart_start = today0 - (days - 1) * DAY
    if since is not None:
        lt_start = time.localtime(start)
        chart_start = time.mktime((lt_start.tm_year, lt_start.tm_mon, lt_start.tm_mday, 0, 0, 0, 0, 0, -1))
        days = max(1, int(round((today0 - chart_start) / DAY)) + 1)
    daily = []
    for i in range(days):
        d0 = chart_start + i * DAY
        daily.append({"day": time.strftime("%m-%d", time.localtime(d0)), "cost": 0.0, "tokens": 0, "turns": 0,
                      "gpt_tokens": 0, "week_pct": None, "by_owner": {}, "by_owner_tok": {}})
    convs: dict[int, dict] = {}
    owners: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("t", 0)):
        t = r.get("t", 0)
        if t < start or t > max(now, round(now, 1)):  # 记录时间按0.1秒取整，允许同一瞬间的取整误差
            continue
        i = min(days - 1, int((t - chart_start) // DAY))
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
            d["by_owner_tok"][r.get("owner", "admin")] = d["by_owner_tok"].get(r.get("owner", "admin"), 0) + tok
        if (r.get("limits") or {}).get("seven_day") is not None:
            d["week_pct"] = r["limits"]["seven_day"]
        num = (groups or {}).get(r["num"], r["num"])
        c = convs.setdefault(num, {"num": num, "name": names.get(num) or r.get("name") or f"#{num}",
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
            "total_cost": round(total, 4), "since": start, "rate": rate}


# ---------- 受限账号的周额度 ----------
WEEK = 7 * DAY
DEFAULT_SHARE = 0.2   # 受限账号默认可用主账号周额度的 1/5（2026-11-04 用户定）


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


def week_start(reset: float | None, now: float | None = None) -> float | None:
    """额度读数给出的周重置时刻（通常是下一次）→ 当前周期起点。"""
    if reset is None:
        return None
    now = now if now is not None else time.time()
    return reset + ((now - reset) // WEEK) * WEEK


def rate(rows: list[dict], since: float, spend=None) -> float | None:
    """“每 1 美元（标价折算）约等于主账号周额度的百分之几”。
    周额度 % 的读数：远窗每轮做完时记的，加上 spend（localspend.LocalSpend）里定时读额度记的。相邻两次读数之间
    周 % 涨了多少 ÷ 这段时间花了多少，累加 since 之后的（看几周的，比只看本周稳）；跨过重置（周 % 变小）的那段不算。
    “花了多少”：有 spend 用本机全部 Claude Code 的消耗（远窗的 + 电脑上直接用的），没有就只算远窗这一轮的。
    claude.ai 网页版、App 的用量看不到，仍会让比例偏大一点（换算出的百分比偏保守）。数据太少返回 None。"""
    pts = [(r["t"], r["limits"]["seven_day"], float(r.get("cost") or 0)) for r in rows
           if r.get("t", 0) >= since and r.get("engine") != "codex" and (r.get("limits") or {}).get("seven_day") is not None]
    if spend is not None:
        pts += [(t, p, 0.0) for t, p in spend.snaps if t >= since]
    pts.sort()
    dp = local_cost = recorded_cost = 0.0
    for (ta, pa, _), (tb, pb, cb) in zip(pts, pts[1:]):
        if pb < pa:
            continue
        dp += pb - pa
        recorded_cost += cb
        if spend is not None:
            local_cost += spend.between(ta, tb)
    # 本机记录按发生时间入账，整轮记录按完成时间入账。同一轮可能跨多个读数区间，
    # 逐段取max会把此前已计入本机记录的消耗在结束时再计一次。整段只选一个来源。
    dc = local_cost if spend is not None and local_cost >= 2 else recorded_cost
    return dp / dc if dc >= 2 and dp >= 1 else None


def quota(rows: list[dict], owner: str, share: float, week_reset: float | None, saved_rate: float | None,
          now: float | None = None, spend=None) -> dict:
    """受限账号本周用了自己额度的百分之几。额度 = 主账号周额度 × share，和主账号同一时间重置。
    pct 是相对它自己的额度（100 = 用完）；used 是折算成主账号周额度的百分比。"""
    now = now or time.time()
    reset = week_reset if week_reset and week_reset > now else None
    start = (reset - WEEK) if reset else now - WEEK
    r = rate(rows, start - 3 * WEEK, spend) or saved_rate
    cost = sum(float(x.get("cost") or 0) for x in rows
               if x.get("t", 0) >= start and x.get("owner") == owner and x.get("engine") != "codex")
    used = cost * r if r else 0.0
    cap = share * 100
    return {"share": share, "cap": round(cap, 1), "cost": round(cost, 4), "used": round(used, 2),
            "pct": round(min(999.0, 100 * used / cap), 1) if cap else 100.0, "rate": r, "calibrating": r is None,
            "reset_at": reset, "start": start}
