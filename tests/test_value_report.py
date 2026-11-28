"""每周“额度换来了什么”：分类、切周、返工 / 白干口径、缓存。不连 DeepSeek。"""
import json

from anzhuo import value_report as v
from anzhuo.value_report import RELAY_HEAD

DAY = 86400


def _r(i, num, t, said, **kw):
    return {"id": f"{num}-{i}", "num": num, "t": t, "ended": t + 60, "said": said, "title": "做完了", **kw}


def test_kind_of_rules():
    assert v.kind_of({"said": RELAY_HEAD + "请写交接"}) == "系统"
    assert v.kind_of({"said": "【回报】#3 做完了"}) == "系统"
    assert v.kind_of({"said": "批准本轮操作：x"}) == "系统"
    assert v.kind_of({"said": "x", "schedule_id": "s1"}) == "系统"
    assert v.kind_of({"said": "（这是接力的新对话……用户这次说：\n\n继续"}) == "系统"
    assert v.kind_of({"said": "（试课反馈）《课》第 1 节「a」第 2 屏：字小"}) == ""   # 试课反馈也让模型判
    assert v.kind_of({"said": "不对，改回去"}) == ""


def test_weeks_anchor_on_real_reset_not_glitches():
    now = 30 * DAY
    snaps = [[1 * DAY, 50], [2 * DAY, 20], [3 * DAY, 60], [10 * DAY, 99], [10 * DAY + 60, 1], [20 * DAY, 70]]
    ws = v.weeks(snaps, now)
    assert ws[-1] == (24 * DAY + 60, now) and all(b - a == 7 * DAY for a, b in ws[:-1])   # 50→20 不算重置


def test_week_report_rework_waste_and_costs():
    spot = "（试课反馈）《鸟》第 2 节「开场：教计算机认鸟」第 3 屏的第 1 步（共 2 步）（topics/bx1/b，节 id s2）：\n字小"
    turns = [_r(1, 1, 100, "做个封面"), _r(2, 1, 200, "不对，颜色错了"), _r(3, 2, 300, spot), _r(4, 2, 400, spot),
             _r(5, 2, 500, "继续", title="出错了", error="You've hit your 5-hour limit · resets 4am"),
             _r(6, 3, 600, "做个表", title="停下了"), _r(7, 3, 700, RELAY_HEAD + "交接")]
    v.mark_repeats(turns)
    assert [r.get("repeat") for r in turns[2:4]] == [False, True]
    labels = {"1-1": "推进", "1-2": "返工", "2-3": "推进", "2-4": "返工", "2-5": "确认", "3-6": "推进", "3-7": "系统"}
    costs = {"1-1": 1.0, "1-2": 1.0, "2-3": 2.0, "2-4": 2.0, "2-5": 1.0, "3-6": 2.0, "3-7": 1.0}
    r = v.week_report(turns, labels, costs, {1: 1, 2: 2, 3: 3}, {1: "封面", 2: "鸟", 3: "表"}, 1.0, 50.0, 0, 1000)
    assert r["said"] == 6 and r["tasks"] == 3 and r["cost"] == 10.0
    assert r["rework"] == 2 and r["rework_share"] == 30 and r["feedback"] == 2   # “不对”一轮 + 判为返工的那条试课反馈
    assert r["wasted"] == 1 and r["wasted_share"] == 20          # 撞额度暂停不算白干，停下的算
    assert r["relay"] == 1 and r["relay_share"] == 10
    assert r["top"][0]["name"] == "鸟" and r["top"][0]["pct"] == 5.0
    assert next(t for t in r["top"] if t["name"] == "表")["cost"] == 3.0   # 交接那轮也算进这件事


def test_classify_caches_and_marks_failures(tmp_path):
    cache = tmp_path / "labels.json"
    recs = [_r(1, 1, 0, "还是不行", prev="改好了"), _r(2, 1, 1, "好的"), _r(3, 1, 2, "【回报】#2 做完了")]
    calls = []

    def ask(system, user):
        calls.append(user)
        if "好的" in user:
            raise TimeoutError
        return "返工"
    out = v.classify(recs, cache, ask)
    assert out == {"1-1": "返工", "1-2": "未判", "1-3": "系统"} and "改好了" in calls[0]
    assert json.loads(cache.read_text()) == {"1-1": "返工"}
    assert v.classify(recs, cache, None) == {"1-1": "返工", "1-2": "未判", "1-3": "系统"}


def test_markdown_marks_live_week_and_top_of_last_full_week():
    base = dict(used_pct=90.0, cost=1, rate=1, said=10, tasks=2, per_task_median=1.0, per_task_mean=2.0, rework=1,
                rework_rate=10, rework_share=5, wasted=0, wasted_share=0, relay=0, relay_share=0, feedback=0, unjudged=0)
    full = {**base, "start": 0, "end": 7 * DAY, "top": [{"name": "封面", "pct": 3.0, "turns": 4, "rework": 1, "cost": 3}]}
    live = {**base, "start": 7 * DAY, "end": 8 * DAY, "top": []}
    text = v.markdown([full, live], now=8 * DAY)
    assert "～现在" in text and "最费的 5 件事" in text and "封面：3.0% 周额度" in text
    assert "返工 10%（花掉 5%）" in v.summary_line(full)


def test_background_weekly_report_stays_local(tmp_path, monkeypatch):
    import asyncio
    import time
    from types import SimpleNamespace

    import pytest
    from anzhuo import housekeeping, value_report

    week = (time.time() - 8 * 86400, time.time() - 7200)
    monkeypatch.setattr(value_report, "weeks", lambda _: [week])
    monkeypatch.setattr(value_report, "deepseek_ask", lambda: pytest.fail("后台统计不应联系 DeepSeek"))
    monkeypatch.setattr(value_report, "build", lambda data_dir, sessions, snaps, spend: [])
    async def sleep(seconds):
        if seconds != 120:
            raise asyncio.CancelledError

    monkeypatch.setattr(housekeeping.asyncio, "sleep", sleep)
    bridge = SimpleNamespace(data_dir=tmp_path, registry=SimpleNamespace(sessions={}),
                             spend=SimpleNamespace(snaps=[]), web=SimpleNamespace(enabled=False))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(housekeeping.value_loop(bridge))
    assert (tmp_path / "value_report.json").is_file()
