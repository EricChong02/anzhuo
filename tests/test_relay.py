"""接力（relay.py 的 Relays）：换家、换型号；额度用完时远窗按记录拼交接材料；总管派的活撞线自动换家。不连 AI。"""
import asyncio
import time

import pytest

from anzhuo import app as appmod
from anzhuo import relay, superagent
from anzhuo.app import Bridge
from anzhuo.config import Config
from anzhuo.engines import CODEX, MODELS
from anzhuo.relay import RELAY
from anzhuo.sessions import Runner, Session

from test_bridge import FakeDT


@pytest.fixture
def codex_ok(monkeypatch):
    monkeypatch.setattr(type(CODEX), "available", lambda self: True)


def _cfg(tmp_path, **kw):
    return Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False, **kw)


def _fake_ai(monkeypatch, runs, relay_reply="## 目标\n做封面", relay_error=""):
    async def fake_run(self, s, job, turn):
        runs.append((s.num, s.engine, job.via, job.text))
        if job.text == RELAY:
            turn.final_text = relay_reply
            if relay_error:
                turn.is_error, turn.error = True, relay_error
            return
        s.claude_id = s.claude_id or f"c{s.num}"
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass
    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(Runner, "refresh_gpt_usage", no_usage)


def _old(b, tmp_path, **kw):
    kw = {"num": 6, "name": "做课封面", "cwd": str(tmp_path), "claude_id": "sid", "model": MODELS["opus"], "effort": "high",
          "context_tokens": 300_000, **kw}
    return b.registry.add(Session(**kw))


def test_handoff_from_records():
    s = Session(num=6, name="做课封面", cwd="/w", add_dirs=["/x"], relay_note="上一棒：先做目录")
    records = [
        {"started": 1, "title": "封面", "said": "（这是接力的新对话：……\n\n---\n用户这次说：\n\n做封面，要醒目",
         "timeline": [{"kind": "you", "t": 1.5, "text": "封面用蓝色"}],
         "final_text": "封面做好了", "changed_files": ["/w/cover.html"]},
        {"started": 2, "request": RELAY, "timeline": [{"kind": "you", "text": RELAY}], "final_text": "内部摘要"},
        {"started": 3, "title": "目录", "request": "再做目录", "timeline": [],   # 老记录：没存完整原话，用标题
         "error": "You've hit your limit", "changed_files": ["/w/toc.html", "/w/cover.html"]},
    ]
    text = relay.handoff(s, records, git="## main\n M toc.html", path="/t/sid.jsonl")
    assert "#6 做课封面" in text and "/w" in text and "/x" in text and "/t/sid.jsonl" in text
    assert "上一棒：先做目录" in text                                     # 更早的交接说明带上
    assert "做封面，要醒目" in text and "封面用蓝色" in text and "再做目录" in text and "这是接力的新对话" not in text
    assert "经总管转来" not in relay.handoff(s, [{"said": "做目录\n\n（经总管转来。先对照……）", "final_text": "好"}])
    assert "内部摘要" not in text and "封面做好了" in text and "出错：You've hit your limit" in text
    assert text.count("- /w/cover.html") == 1 and "- /w/toc.html" in text   # 改过的文件汇总、去重
    assert "M toc.html" in text and "做过的别重做" in text


def test_targets(tmp_path, codex_ok):
    b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
    s = _old(b, tmp_path)
    assert [t["to"] for t in b.relays.targets(s)] == ["sonnet", "gpt"]
    s.limit_until = time.time() + 3600                                    # Claude 撞线：同一家换了也接不上
    assert [t["to"] for t in b.relays.targets(s)] == ["gpt"]
    b.quota_levels["gpt"] = "撞线"
    assert b.relays.targets(s) == [] and "换不了" in b.relays.refuse(s, "gpt") and "同一家" in b.relays.refuse(s)
    g = _old(b, tmp_path, num=7, engine="codex", claude_id=None, codex_id="x", model=None)
    assert [t["to"] for t in b.relays.targets(g)] == ["opus", "sonnet"]
    assert [t["to"] for t in b.relays.targets(_old(b, tmp_path, num=8, root=str(tmp_path)))] == ["sonnet"]   # 受限账号只用 Claude


def test_capped_relays_with_handoff_and_goes_on(tmp_path, monkeypatch, codex_ok):
    """撞线了：不让原对话写（写不了），远窗拼材料，换 GPT 开新对话，马上替你说“继续”；原对话到点也不再自动接着做。"""
    runs = []
    _fake_ai(monkeypatch, runs)

    async def go():
        b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
        old = _old(b, tmp_path, limit_until=time.time() + 7200, relay_pending={})
        msg = await b.relays.start(6, to="gpt", go=True)
        new = b.registry.get(old.relay_to)
        assert "已接力到" in msg and new.engine == "codex" and new.model is None and new.effort is None
        assert new.relay_from == 6 and "额度用完了" in new.unsent and "交接材料" in new.unsent and "Claude" not in new.unsent.split("。")[0][:5]
        await asyncio.sleep(0.2)
        assert [r[3].endswith("用户这次说：\n\n继续") for r in runs] == [True] and runs[0][:2] == (new.num, "codex")
        assert not any(r[3] == RELAY for r in runs)
    asyncio.run(go())


def test_switch_model_writes_note(tmp_path, monkeypatch, codex_ok):
    """没撞线换 Sonnet：照旧让原对话写交接说明，新对话换型号、深度保留（同一家）。"""
    runs = []
    _fake_ai(monkeypatch, runs)

    async def go():
        b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
        old = _old(b, tmp_path, context_tokens=1000)                    # 没有接力建议也能换（你点名换的）
        assert "交接说明" in await b.relays.start(6, to="sonnet")
        await asyncio.sleep(0.2)
        new = b.registry.get(old.relay_to)
        assert new.model == MODELS["sonnet"] and new.effort == "high" and new.relay_note == "## 目标\n做封面"
        assert "用户要换 Sonnet" in new.unsent and len(runs) == 1      # 没让新对话自己开工
    asyncio.run(go())


def test_note_hits_limit_falls_back_to_handoff(tmp_path, monkeypatch, codex_ok):
    """写交接说明时撞了额度：改由远窗按记录拼，照样开新对话。"""
    runs = []
    _fake_ai(monkeypatch, runs, relay_reply="", relay_error="You've hit your session limit · resets 5:40pm")
    monkeypatch.setattr(appmod, "limit_reset", lambda text, now=None: time.time() + 7200 if "limit" in (text or "") else None)

    async def go():
        b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
        old = _old(b, tmp_path)
        await b.relays.start(6)
        await asyncio.sleep(0.2)
        new = b.registry.get(old.relay_to)
        assert new and new.model == MODELS["opus"] and "交接材料" in new.relay_note and "额度用完了" in new.unsent
        assert 6 not in b.relays.writing
    asyncio.run(go())


def test_router_worker_hits_limit_switches(tmp_path, monkeypatch, codex_ok):
    """总管派的活撞线、要等很久：自动换另一家接力接着做，新对话照样回报总管；总管收到一条说明。"""
    runs = []

    async def fake_run(self, s, job, turn):
        runs.append((s.num, s.engine, job.via, job.text[:30]))
        if s.role == "router":
            turn.final_text = "好的"
        elif s.engine == "claude":
            s.claude_id = "sid"
            turn.is_error, turn.error = True, "You've hit your session limit"
        else:
            s.codex_id = "cid"
            turn.final_text = "改好了"

    async def no_usage(self, force=False):
        pass
    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(Runner, "refresh_gpt_usage", no_usage)
    monkeypatch.setattr(appmod, "limit_reset", lambda text, now=None: time.time() + 3 * 3600 if "limit" in (text or "") else None)

    async def go():
        b = Bridge(_cfg(tmp_path, super_agent=True), FakeDT(), data_dir=tmp_path / "data")
        boss = superagent.find(b)
        w = b.registry.add(Session(num=b.registry.reserve_num(), name="活", cwd=str(tmp_path), boss=boss.num, model=MODELS["sonnet"]))
        await b.submit(w, "做件事", superagent._incoming("做件事", superagent.VIA))
        for _ in range(40):
            await asyncio.sleep(0.1)
            if any(r[0] == boss.num for r in runs):
                break
        new = b.registry.get(w.relay_to)
        assert new and new.engine == "codex" and new.boss == boss.num
        told = [r for r in runs if r[0] == boss.num]
        assert told and "接力到了" in told[0][3] and any(r[0] == new.num and r[1] == "codex" for r in runs)
    asyncio.run(go())
