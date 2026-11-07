"""总管（super_agent）：默认不开；开了以后派活、结构化汇报、退回、直投、重启恢复、名额都走一遍。不连 Claude。"""
import asyncio
import json

from anzhuo import superagent
from anzhuo.app import Bridge
from anzhuo.config import Config, load_config
from anzhuo.dingtalk import Incoming
from anzhuo.sessions import Runner, Session

from test_bridge import FakeDT


def _cfg(tmp_path, on=True, **kw):
    return Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"),
                  web_base="https://xe.example", folders={"项目": str(tmp_path / "proj")}, super_agent=on, **kw)


def _tools(b, s=None):
    s = s or superagent.find(b)
    return s, {t.name: t.handler for t in superagent.role(b, s)["tools"]}


def _out(res):
    return res["content"][0]["text"]


def web(t):
    return Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text=t, via="web")


def _setup(monkeypatch, runs, b_ref, script=None):
    """假 AI：记下谁、从哪、说了什么；worker 按 script(s, job) 调结构化汇报 / 退回。"""
    async def fake_run(self, s, job, turn):
        runs.append((s.num, job.via, job.text))
        if s.role == "router":
            turn.final_text = "好的"
            return
        turn.final_text = "改好了"
        turn.changed_files.append("/tmp/a.py")
        if script and (act := script(s, job)):
            _, t = _tools(b_ref[0], s)
            await t[act[0]](act[1])

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)


def _bridge(tmp_path, b_ref, pushes, **kw):
    b = Bridge(_cfg(tmp_path, **kw), FakeDT(), data_dir=tmp_path / "data")
    b_ref.append(b)

    async def fake_push(title, body, num=None, only=None):
        pushes.append(num)
        return 1
    b.web.push.send = fake_push
    return b


def test_off_by_default(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    assert load_config(p).super_agent is False
    p.write_text("super_agent = true\n")
    assert load_config(p).super_agent is True
    b = Bridge(_cfg(tmp_path, on=False), FakeDT(), data_dir=tmp_path / "data")
    assert superagent.find(b) is None and b.registry.sessions == {}
    assert b.runner.role(None) is None and not b.runner.use_spare
    s = Session(num=9, name="x", cwd="/tmp", boss=1)
    assert b.runner.role(s) is None   # 关着：派出去的对话也没有汇报工具


def test_router_created_once(tmp_path):
    b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
    r = superagent.find(b)
    assert r and r.role == "router" and r.pinned and r.engine == "claude" and r.name == "总管"
    role = superagent.role(b, r)
    assert {"Bash", "Edit", "Write", "WebSearch"} <= set(role["disallowed"]) and role["own_slot"] and "总管" in role["append"]
    assert "项目" in role["append"]   # 文件夹短名告诉了总管
    assert {t.name for t in role["tools"]} == {"list_sessions", "new_session", "send_to_session", "session_status", "stop_session"}
    b2 = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")   # 重启：不再开第二个
    assert [s.num for s in b2.registry.sessions.values() if s.role == "router"] == [r.num]
    assert b.runner.use_spare


def test_deny_hook_blocks_router_tools():
    r = Runner(None, None)
    hook = r._deny({"Bash", "Write"}, "不亲自干活")
    out = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "不亲自干活" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert asyncio.run(hook({"tool_name": "Read", "tool_input": {}}, None, None)) == {}


def test_dispatch_report_and_question(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    runs, b_ref, pushes = [], [], []

    def script(s, job):
        if "起名" in job.text:
            return ("report", {"state": "waiting", "summary": "列了三个名字", "question": "选哪个？A. 甲 B. 乙"})
        return ("report", {"state": "ready", "summary": "登录页修好了"})
    _setup(monkeypatch, runs, b_ref, script)

    async def go():
        b = _bridge(tmp_path, b_ref, pushes)
        router, t = _tools(b)
        assert "已开 #" in _out(await t["new_session"]({"task": "修一下登录页", "folder": "项目"}))
        await asyncio.sleep(0.2)
        w = next(s for s in b.registry.sessions.values() if s.role != "router")
        assert w.boss == router.num and w.cwd == str(tmp_path / "proj") and w.ticket == "ready"
        assert {x.name for x in superagent.role(b, w)["tools"]} == {"report", "handback"}
        rep = [r for r in runs if r[0] == router.num]
        assert rep and rep[0][1] == "report" and rep[0][2].startswith("【回报】") and "做完了：登录页修好了" in rep[0][2]
        assert pushes == [router.num]                       # 派出去的那一轮不直接推给你

        # 要用户定：一问就转给总管；这一轮结束不再让总管跑第二轮
        runs.clear(), pushes.clear()
        await t["new_session"]({"task": "给记账工具起名"})
        await asyncio.sleep(0.3)
        q = next(s for s in b.registry.sessions.values() if "起名" in s.name)
        assert q.awaiting == "选哪个？A. 甲 B. 乙" and q.awaiting_kind == "decide" and q.ticket == "asked"
        to_router = [r[2] for r in runs if r[0] == router.num]
        assert len(to_router) == 1 and to_router[0].startswith("【要用户定】") and "B. 乙" in to_router[0]
        assert any("在等用户定" in n for n in router.notes)  # 记一笔，不再跑

        listed = json.loads(_out(await t["list_sessions"]({})))
        assert {x["num"]: x["state"] for x in listed}[q.num] == "等用户定"
        st = json.loads(_out(await t["session_status"]({"num": w.num})))
        assert st["reply"] == "改好了"
        assert (await t["send_to_session"]({"num": router.num, "text": "x"}))["isError"]
        assert (await t["send_to_session"]({"num": 999, "text": "x"}))["isError"]

    asyncio.run(go())


def test_human_direct_turn_notes_router(tmp_path, monkeypatch):
    runs, b_ref, pushes = [], [], []
    _setup(monkeypatch, runs, b_ref, lambda s, job: ("report", {"state": "ready", "summary": "又改了一处"}))

    async def go():
        b = _bridge(tmp_path, b_ref, pushes)
        router, t = _tools(b)
        await t["new_session"]({"task": "修 bug"})
        await asyncio.sleep(0.2)
        w = next(s for s in b.registry.sessions.values() if s.role != "router")
        runs.clear(), pushes.clear()
        await b._submit(w, "顺便改下颜色", web("顺便改下颜色"))     # 你直接在 worker 里说
        await asyncio.sleep(0.2)
        assert [r[0] for r in runs] == [w.num] and pushes == [w.num]   # 总管不跑，直接通知你
        assert any("用户直接说的" in n and "又改了一处" in n for n in router.notes)
        await b._submit(router, "现在怎么样", web("现在怎么样"))  # 总管开口前带上“期间的情况”，然后清空
        await asyncio.sleep(0.2)
        said = [r[2] for r in runs if r[0] == router.num][-1]
        assert said.startswith("【期间的情况】") and said.endswith("现在怎么样") and router.notes == []

    asyncio.run(go())


def test_direct_hash_goes_to_worker(tmp_path, monkeypatch):
    runs, b_ref, pushes = [], [], []
    _setup(monkeypatch, runs, b_ref, lambda s, job: ("report", {"state": "ready", "summary": "按 B 做了"}))

    async def go():
        b = _bridge(tmp_path, b_ref, pushes)
        router, t = _tools(b)
        await t["new_session"]({"task": "起名"})
        await asyncio.sleep(0.2)
        w = next(s for s in b.registry.sessions.values() if s.role != "router")
        runs.clear()
        await b._submit(router, f"#{w.num} 选 B", web(f"#{w.num} 选 B"))
        await asyncio.sleep(0.2)
        assert runs[0][:2] == (w.num, "router") and runs[0][2].startswith("选 B") and superagent.FOLLOW in runs[0][2]   # 直接交给 worker，总管没跑
        assert runs[1][0] == router.num and runs[1][2].startswith("【期间的情况】")   # 它的回报到总管时带着“用户直接说的”
        assert "用户直接对" in runs[1][2] and "【回报】" in runs[1][2]
        runs.clear()
        await b._submit(router, "#999 你好", web("#999 你好"))   # 没这个对话：照常交给总管
        await asyncio.sleep(0.2)
        assert runs and runs[0][0] == router.num

    asyncio.run(go())


def test_handback(tmp_path, monkeypatch):
    runs, b_ref, pushes = [], [], []

    def script(s, job):
        if "天气" in job.text:
            return ("handback", {"reason": "这是另一件事"})
        return ("report", {"state": "ready", "summary": "好了"})
    _setup(monkeypatch, runs, b_ref, script)

    async def go():
        b = _bridge(tmp_path, b_ref, pushes)
        router, t = _tools(b)
        await t["new_session"]({"task": "修 bug"})
        await asyncio.sleep(0.2)
        w = next(s for s in b.registry.sessions.values() if s.role != "router")
        runs.clear(), pushes.clear()
        await t["send_to_session"]({"num": w.num, "text": "查下天气"})
        await asyncio.sleep(0.2)
        back = [r[2] for r in runs if r[0] == router.num]
        assert back and back[0].startswith("【退回】") and "这是另一件事" in back[0] and "查下天气" in back[0]
        assert w.num not in pushes
        assert (await t["send_to_session"]({"num": w.num, "text": "查下天气"}))["isError"]   # 不能再交回去

    asyncio.run(go())


def test_no_structured_report_falls_back(tmp_path, monkeypatch):
    runs, b_ref, pushes = [], [], []
    _setup(monkeypatch, runs, b_ref, None)   # worker 没调 report（比如 Codex）

    async def go():
        b = _bridge(tmp_path, b_ref, pushes)
        router, t = _tools(b)
        await t["new_session"]({"task": "修 bug"})
        await asyncio.sleep(0.2)
        rep = [r[2] for r in runs if r[0] == router.num]
        assert rep and "没按格式汇报" in rep[0]

    asyncio.run(go())


def test_restart_recovers_dispatched_work(tmp_path, monkeypatch):
    runs, b_ref, pushes = [], [], []
    _setup(monkeypatch, runs, b_ref, lambda s, job: ("report", {"state": "ready", "summary": "接着做完了"}))
    b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
    router = superagent.find(b)
    w = Session(num=b.registry.reserve_num(), name="长活", cwd=str(tmp_path), boss=router.num, status="running", claude_id="c1")
    mine = Session(num=b.registry.reserve_num(), name="你自己的", cwd=str(tmp_path), status="running", claude_id="c2")
    b.registry.add(w), b.registry.add(mine)
    router.status = "running"
    b.registry.save()

    async def go():
        b2 = _bridge(tmp_path, b_ref, pushes)                       # 模拟重启
        assert sorted(b2.interrupted) == sorted([router.num, w.num, mine.num])
        await superagent.recover(b2, b2.interrupted)
        await asyncio.sleep(0.3)
        assert any(r[0] == w.num and "远窗重启了" in r[2] for r in runs)
        assert not any(r[0] == mine.num for r in runs)              # 不是总管派的：不自动接着做
        assert any(r[0] == router.num and "【重启】" in r[2] for r in runs)

    asyncio.run(go())


def test_slots(tmp_path):
    """总管有自己的名额；你接着跟已有对话说话能用预留名额；等名额时算在忙，叫停了就不做。"""
    async def go():
        r = Runner(None, None, max_parallel=1)
        r.use_spare = True
        await r.sem.acquire()                          # 名额满了
        async with r._slot(own=True, human=False):     # 总管不受影响
            pass
        async with r._slot(own=False, human=True):     # 你接着说话：用预留的
            assert r.spare.locked()
        r.use_spare = False
        t = asyncio.create_task(r._slot(own=False, human=True).__aenter__())
        await asyncio.sleep(0.05)
        assert not t.done()                            # 没开预留：照样排队
        t.cancel()

    asyncio.run(go())


def test_waiting_counts_busy_and_can_stop(tmp_path, monkeypatch):
    runs, b_ref, pushes = [], [], []
    _setup(monkeypatch, runs, b_ref, None)

    async def go():
        b = _bridge(tmp_path, b_ref, pushes, max_parallel=1)
        await b.runner.sem.acquire()                   # 名额被别的占着
        s = Session(num=b.registry.reserve_num(), name="排队的", cwd=str(tmp_path))
        b.registry.add(s)
        b.runner.submit(s, __import__("anzhuo.sessions", fromlist=["Job"]).Job(text="做点事", via="router"))
        await asyncio.sleep(0.1)
        assert b.runner.busy(s.num) and superagent.state(b, s) == "排队"
        assert await b.runner.stop(s.num)
        b.runner.sem.release()
        await asyncio.sleep(0.1)
        assert not any(r[0] == s.num for r in runs) and not b.runner.busy(s.num)

    asyncio.run(go())


def test_report_text():
    from anzhuo.report import Turn
    s = Session(num=3, name="登录页", cwd="/tmp")
    t = Turn(started=0, request="x", final_text="好了" * 3000)
    t.changed_files = ["/a/b.py", "/a/b.py", "/a/c.py"]
    out = superagent.report_text(s, t, {"state": "ready", "summary": "修好了", "question": ""})
    assert out.startswith("【回报】#3 登录页：做完了：修好了；改了 b.py、c.py") and "太长截掉了" in out
    assert "在等用户定：要 A 还是 B？" in superagent.report_text(s, t, {"state": "waiting", "summary": "", "question": "要 A 还是 B？\nA. x"})
    t.is_error, t.error = True, "额度用完"
    assert "出错了" in superagent.report_text(s, t)


def test_must_report_nudges_once():
    from anzhuo.report import Turn
    t = Turn(started=0, via="router")
    assert superagent.must_report(t, False) == superagent.NUDGE   # 总管派的、没汇报：拦一次
    assert superagent.must_report(t, True) == ""                  # 已经拦过：放它结束
    t.sa_report = {"state": "ready", "summary": "x", "question": ""}
    assert superagent.must_report(t, False) == ""
    assert superagent.must_report(Turn(started=0, via="web"), False) == ""   # 你自己直接说的：不强求
    r = Runner(None, None)
    hook = r._stop_check(Turn(started=0, via="router"), superagent.must_report)
    assert asyncio.run(hook({"stop_hook_active": False}, None, None))["decision"] == "block"
    assert asyncio.run(hook({"stop_hook_active": True}, None, None)) == {}
