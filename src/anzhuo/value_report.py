"""每周“额度换来了什么”：按周额度的周期，看你说了多少话、干了几件事、每件花了多少周额度、返工和白干占了多少。

口径（2026-11-28 和定）：
- 一件事 = 一条对话链（接力的算同一件）；这周里你在它里面说过话就算这周的一件。
- 花了多少 = 每轮记的 API 标价折算金额 × 换算比例（usage.rate：1 美元约等于周额度百分之几）。只有 Claude 有金额，GPT 只计轮数。
- 返工 = 你这句话是在说上一轮没做对、没做到、做坏了、要改回或重做（DeepSeek 判一次，结果缓存在 value_labels.json）。
  试课反馈也让它判：同一屏这周之前反馈过的会告诉它，但对同一屏提新的修改想法不算返工。
- 白干 = 出错、被你停下的轮次（撞额度停下的不算：恢复后接着做）；接力写交接说明算“接力开销”，单列。
- 判不了的（没有 DeepSeek 密钥、调用失败）记为“未判”，不算返工，表里写明有几条。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from statistics import median

from . import usage

LABELS = ("返工", "推进", "确认", "提问")
SYSTEM = """你在给用户发给 AI 编程助手的一句话分类。会给你助手上一轮回复的结尾和用户这句话。只回一个词：
返工：用户在说上一轮的结果不对、没做到要求、做坏了、有 bug、看错了、要改回或重做（如“不对”“还是不行”“咋串台了”“没改啊”“刚才那个又坏了”）。
推进：新的要求、下一步、补充新信息、对已有东西提新的修改想法（不是因为上一轮做错）。
确认：同意、选选项、批准、让它继续。
提问：问情况、问原因、问怎么办，没有指出上一轮做错。
只回 返工 / 推进 / 确认 / 提问 之一。"""
TITLE = re.compile(r"《([^》]+)》")
SPOT = re.compile(r"第 \d+ 节「[^」]*」第 \d+ 屏")   # 一批里可能有好几处（“共 N 处”一行一处）
RELAY_HEAD = "这个对话太长了，要换到一个新对话接着做。"


def kind_of(rec: dict) -> str:
    """不用问模型就能定的：系统发的（交接、回报、批准、定时任务）、试课反馈 / 提问；其余返回空。"""
    said = (rec.get("said") or rec.get("request") or "").strip()
    if rec.get("schedule_id") or said.startswith((RELAY_HEAD, "【回报】", "批准本轮操作", "批准本会话")):
        return "系统"
    if said.startswith("（这是接力的新对话") and "用户这次说：\n\n" in said:
        said = said.split("用户这次说：\n\n", 1)[1].strip()
        if said in ("继续", ""):
            return "系统"
    if said.startswith("（试课提问）"):
        return "提问"
    return ""


def _prompt(r: dict) -> str:
    said = r.get("said") or r.get("request") or ""
    if "用户这次说：\n\n" in said:
        said = said.split("用户这次说：\n\n", 1)[1]
    hint = "\n（这是试课页上对某一屏的反馈；这一屏这周之前已经反馈过。）" if r.get("repeat") else ""
    return f"助手上一轮回复的结尾：\n{(r.get('prev') or '（没有，这是第一句）')[-500:]}\n\n用户这句话：{hint}\n{said[:800]}"


def mark_repeats(records: list[dict]) -> None:
    """试课反馈：同一门课同一屏这周之前反馈过的，记 repeat（给判返工的提示用）。records 按时间。"""
    seen = set()
    for r in sorted(records, key=lambda r: r["t"]):
        said = r.get("said") or ""
        if not said.startswith("（试课反馈）") or not (m := TITLE.search(said)):
            continue
        keys = {(m.group(1), x) for x in SPOT.findall(said)}
        r["repeat"] = bool(keys & seen)
        seen |= keys


def classify(records: list[dict], cache_path: Path, ask=None, workers: int = 8) -> dict[str, str]:
    """每轮 id → 类别。records 带 prev（同一对话上一轮回复）；ask(system, user) → 一个词，None = 不调模型。
    判过的存进 cache_path，下次不再问；判不了的记“未判”，下次再试。"""
    from concurrent.futures import ThreadPoolExecutor
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    out, todo = {}, []
    for r in records:
        if k := kind_of(r):
            out[r["id"]] = k
        elif r["id"] in cache:
            out[r["id"]] = cache[r["id"]]
        else:
            out[r["id"]] = "未判"
            todo.append(r)
    if ask and todo:
        def one(r):
            try:
                word = ask(SYSTEM, _prompt(r)).strip()
            except Exception:  # noqa: BLE001 - 判不了就记未判
                return r["id"], None
            return r["id"], next((x for x in LABELS if x in word), "推进")
        with ThreadPoolExecutor(workers) as pool:
            for rid, word in pool.map(one, todo):
                if word:
                    out[rid] = cache[rid] = word
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False))
        tmp.replace(cache_path)
    return out


def weeks(snaps: list, now: float | None = None) -> list[tuple[float, float]]:
    """周额度的周期：以最近一次重置（读数从高处掉到 10% 以下）为锚，往前每 7 天一段，最后一段到现在。
    不按每次读数下跌切：11-25 修好读额度之前有几次读错的下跌，按它切会切出两三天的“周”。"""
    now = now or time.time()
    snaps = sorted(snaps)
    resets = [t for (_, a), (t, b) in zip(snaps, snaps[1:]) if a - b > 5 and b < 10]
    anchor = resets[-1] if resets else now - usage.WEEK
    ahead = int((now - anchor) // usage.WEEK)   # 锚之后又过了几整周（重置时远窗没在读额度也照样往后切）
    starts = [anchor + k * usage.WEEK for k in range(-8, ahead + 1)]
    return [(a, b) for a, b in zip(starts, starts[1:] + [now]) if a < now]


def load_turns(turns_dir: Path, start: float, end: float) -> list[dict]:
    """[start, end) 里的轮次记录，按对话、时间排好，每条带上同一对话上一轮回复（prev）。前一周的最后一轮也读，给 prev 用。"""
    recs = []
    for p in turns_dir.glob("*.json"):
        try:
            if p.stat().st_mtime < start - 7 * 86400:
                continue
            r = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        r["num"] = int(str(r.get("id", "0-")).split("-")[0] or 0)
        r["t"] = float(r.get("started") or 0)
        if r["t"] < end:
            recs.append(r)
    recs.sort(key=lambda r: (r["num"], r["t"]))
    out, last = [], {}
    for r in recs:
        r["prev"] = last.get(r["num"], "")
        last[r["num"]] = r.get("final_text") or ""
        if r["t"] >= start:
            out.append(r)
    return out


def week_report(turns: list[dict], labels: dict[str, str], costs: dict[str, float], groups: dict[int, int],
                names: dict[int, str], rate: float | None, used_pct: float | None, start: float, end: float) -> dict:
    """一周的数。costs：轮次 id → 标价美元；groups：对话 → 接力链的头；rate：1 美元 ≈ 周额度百分之几。"""
    pct = (lambda d: round(d * rate, 1)) if rate else (lambda d: None)
    total = sum(costs.get(r["id"], 0.0) for r in turns)
    rework, tasks = set(), {}
    for r in sorted(turns, key=lambda r: r["t"]):
        k = labels.get(r["id"], "未判")
        if k == "返工":
            rework.add(r["id"])
        if k not in ("系统",):
            g = groups.get(r["num"], r["num"])
            t = tasks.setdefault(g, {"name": names.get(g) or names.get(r["num"]) or f"#{g}", "turns": 0, "rework": 0, "cost": 0.0})
            t["turns"] += 1
            t["rework"] += r["id"] in rework
    for r in turns:   # 接力、回报这些系统轮次的花费也算进它那件事
        if (g := groups.get(r["num"], r["num"])) in tasks:
            tasks[g]["cost"] += costs.get(r["id"], 0.0)
    said = [r for r in turns if labels.get(r["id"]) != "系统"]
    # 撞了 5 小时 / 周额度停下的不算白干：额度恢复后接着做，前面做的都还在
    capped = lambda r: "hit your" in (r.get("error") or "") and "limit" in (r.get("error") or "")
    wasted = [r for r in turns if (r.get("title") in ("出错了", "停下了") or r.get("error")) and not capped(r)]
    relay = [r for r in turns if (r.get("said") or "").startswith(RELAY_HEAD)]
    cost_of = lambda rs: sum(costs.get(r["id"], 0.0) for r in rs)
    per_task = [t["cost"] for t in tasks.values()]
    top = sorted(tasks.values(), key=lambda t: -t["cost"])[:5]
    share = lambda rs: round(100 * cost_of(rs) / total) if total else 0
    rw = [r for r in turns if r["id"] in rework]
    return {
        "start": start, "end": end, "used_pct": used_pct, "cost": round(total, 1), "rate": rate,
        "said": len(said), "tasks": len(tasks),
        "per_task_median": pct(median(per_task)) if per_task else None,
        "per_task_mean": pct(sum(per_task) / len(per_task)) if per_task else None,
        "rework": len(rw), "rework_rate": round(100 * len(rw) / len(said)) if said else 0, "rework_share": share(rw),
        "wasted": len(wasted), "wasted_share": share(wasted), "relay": len(relay), "relay_share": share(relay),
        "feedback": sum((r.get("said") or "").startswith("（试课反馈）") for r in turns),
        "unjudged": sum(labels.get(r["id"]) == "未判" for r in turns),
        "top": [{**t, "cost": round(t["cost"], 1), "pct": pct(t["cost"])} for t in top],
    }


def turn_costs(turns: list[dict], sessions: dict) -> dict[str, float]:
    """每轮花了多少（API 标价折算美元）：读这个对话的 Claude 记录（含子代理），把每条回复按时间归到那一轮。
    不用 usage.jsonl 每轮记的差值：那是 Claude 报的会话累计相减，远窗重启后接着、分叉的对话会把整段累计算进一轮。"""
    from .localspend import cost
    from .thread import transcript_path
    by_num: dict[int, list[dict]] = {}
    for r in turns:
        by_num.setdefault(r["num"], []).append(r)
    out = {}
    for num, rs in by_num.items():
        s = sessions.get(num)
        path = transcript_path(s.claude_id) if s and getattr(s, "claude_id", None) else None
        if not path:
            continue
        spans = sorted((r["t"], float(r.get("ended") or r["t"]) + 5, r["id"]) for r in rs)
        lo, hi = spans[0][0], spans[-1][1]
        files = [path, *(path.parent / s.claude_id / "subagents").glob("*.jsonl")]
        seen = set()
        for f in files:
            try:
                lines = f.open(errors="ignore")
            except OSError:
                continue
            with lines:
                for ln in lines:
                    if '"usage"' not in ln or '"assistant"' not in ln:
                        continue
                    try:
                        m = json.loads(ln)
                    except ValueError:
                        continue
                    msg = m.get("message") or {}
                    if m.get("type") != "assistant" or not msg.get("usage") or msg.get("id") in seen:
                        continue
                    seen.add(msg.get("id"))
                    try:
                        ts = _epoch(m["timestamp"])
                    except (KeyError, ValueError):
                        continue
                    if not lo <= ts <= hi:
                        continue
                    rid = next((i for a, b, i in spans if a <= ts <= b), None)
                    if rid:
                        out[rid] = out.get(rid, 0.0) + cost(msg.get("model") or "", msg["usage"])
    return out


def _epoch(ts: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def build(data_dir: Path, sessions: dict, snaps: list, spend=None, ask=None, now: float | None = None, n: int = 4) -> list[dict]:
    """最近 n 个周期的报告（旧的在前）。sessions：编号 → Session；ask 见 classify。"""
    now = now or time.time()
    rows = usage.load(data_dir / "usage.jsonl", now - 8 * usage.WEEK)
    rate = usage.rate(rows, now - 4 * usage.WEEK, spend)
    groups = usage.relay_groups(sessions)
    names = {k: s.name for k, s in sessions.items()}
    out = []
    for start, end in weeks(snaps, now)[-n:]:
        turns = load_turns(data_dir / "turns", start, end)
        mark_repeats(turns)
        labels = classify(turns, data_dir / "value_labels.json", ask)
        used = max((p for t, p in snaps if start <= t < end), default=None)
        out.append(week_report(turns, labels, turn_costs(turns, sessions), groups, names, rate, used, start, end))
    return out


def deepseek_ask():
    """有 DeepSeek 密钥就返回 ask(system, user)；没有返回 None（全记未判）。"""
    from . import deepseek_api
    key = deepseek_api.key()
    if not key:
        return None
    return lambda system, user: deepseek_api.chat(system, user, key, timeout=20, temperature=0, max_tokens=8, purpose="value_report")


def markdown(reps: list[dict], now: float | None = None) -> str:
    now = now or time.time()
    day = lambda t: time.strftime("%m-%d", time.localtime(t))
    f = lambda v, unit="%": "—" if v is None else f"{v}{unit}"
    live = lambda r: now - r["end"] < 3600
    cols = [f"{day(r['start'])}～{'现在' if live(r) else day(r['end'])}" for r in reps]
    rows = [("周额度用到", [f(r["used_pct"]) for r in reps]),
            ("你说的话（轮）", [str(r["said"]) for r in reps]),
            ("干的事（对话）", [str(r["tasks"]) for r in reps]),
            ("平均每件花的周额度", [f(r["per_task_mean"]) for r in reps]),
            ("返工（轮 · 占你说的话）", [f"{r['rework']} · {r['rework_rate']}%" for r in reps]),
            ("返工花掉的", [f"{r['rework_share']}%" for r in reps]),
            ("出错、停下（轮 · 花掉的）", [f"{r['wasted']} · {r['wasted_share']}%" for r in reps]),
            ("接力写交接（轮 · 花掉的）", [f"{r['relay']} · {r['relay_share']}%" for r in reps])]
    out = ["| | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    out += [f"| {k} | " + " | ".join(v) + " |" for k, v in rows]
    done = [(c, r) for c, r in zip(cols, reps) if not live(r)]
    if done and done[-1][1]["top"]:
        c, r = done[-1]
        out += ["", f"{c} 最费的 5 件事："]
        out += [f"- {t['name']}：{f(t['pct'])} 周额度，{t['turns']} 轮，返工 {t['rework']} 轮" for t in r["top"]]
    if any(r["unjudged"] for r in reps):
        out += ["", "未判：" + "、".join(f"{c} {r['unjudged']} 条" for c, r in zip(cols, reps) if r["unjudged"]) + "（不算返工）"]
    return "\n".join(out)


def summary_line(r: dict) -> str:
    """推送通知用的一句话。"""
    f = lambda v: "—" if v is None else f"{v}%"
    return (f"周额度用到 {f(r['used_pct'])}，干了 {r['tasks']} 件事，平均每件 {f(r['per_task_mean'])}；"
            f"返工 {r['rework_rate']}%（花掉 {r['rework_share']}%）")


def save(path: Path, reps: list[dict], now: float | None = None) -> dict:
    data = {"at": now or time.time(), "weeks": reps, "md": markdown(reps, now)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)
    return data


def saved(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
