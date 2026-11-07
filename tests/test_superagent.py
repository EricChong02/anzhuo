"""总管（super_agent）：默认不开；开了以后派活、转达、回报都走一遍。不连 Claude。"""
import asyncio
import json

from anzhuo import superagent
from anzhuo.app import Bridge
from anzhuo.config import Config, load_config
from anzhuo.dingtalk import Incoming
from anzhuo.sessions import Runner

from test_bridge import FakeDT


def _cfg(tmp_path, on=True):
    return Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"),
                  web_base="https://xe.example", folders={"项目": str(tmp_path / "proj")}, super_agent=on)


def _tools(b):
    router = superagent.find(b)
    return router, {t.name: t.handler for t in superagent.role(b, router)["tools"]}


def _out(res):
    return res["content"][0]["text"]


def _setup(monkeypatch, runs):
    async def fake_run(self, s, job, turn):
        runs.append((s.num, job.via, job.text))
        turn.final_text = "改好了" if s.role != "router" else "好的"
        turn.changed_files.append("/tmp/a.py") if s.role != "router" else None

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)


def test_off_by_default(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text("")
    assert load_config(p).super_agent is False
    p.write_text("super_agent = true\n")
    assert load_config(p).super_agent is True
    b = Bridge(_cfg(tmp_path, on=False), FakeDT(), data_dir=tmp_path / "data")
    assert superagent.find(b) is None and b.registry.sessions == {}
    assert b.runner.role(None) is None


def test_router_created_once(tmp_path):
    b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
    r = superagent.find(b)
    assert r and r.role == "router" and r.pinned and r.engine == "claude" and r.name == "总管"
    role = superagent.role(b, r)
    assert "Bash" in role["disallowed"] and "Edit" in role["disallowed"] and "总管" in role["append"]
    assert {t.name for t in role["tools"]} == {"list_sessions", "new_session", "send_to_session", "session_status", "stop_session"}
    b2 = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")   # 重启：不再开第二个
    assert [s.num for s in b2.registry.sessions.values() if s.role == "router"] == [r.num]
    assert superagent.role(b, b.registry.get(r.num + 99)) is None


def test_dispatch_and_report(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    runs = []
    _setup(monkeypatch, runs)
    pushes = []

    async def go():
        b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")

        async def fake_push(title, body, num=None, only=None):
            pushes.append((num, body))
            return 1
        b.web.push.send = fake_push
        router, t = _tools(b)

        res = await t["new_session"]({"task": "修一下登录页", "folder": "项目"})
        assert "已开 #" in _out(res)
        await asyncio.sleep(0.2)
        w = next(s for s in b.registry.sessions.values() if s.role != "router")
        assert w.boss == router.num and w.cwd == str(tmp_path / "proj")
        # 派出去的对话做完：先回报给总管（via=report），不直接推给用户；总管那一轮做完才推给用户
        assert (w.num, "router", "修一下登录页") in runs
        report = [r for r in runs if r[0] == router.num]
        assert report and report[0][1] == "report" and "【回报】" in report[0][2] and "做完了" in report[0][2]
        assert [n for n, _ in pushes] == [router.num]

        listed = json.loads(_out(await t["list_sessions"]({})))
        assert [x["num"] for x in listed] == [w.num] and listed[0]["from_router"]
        st = json.loads(_out(await t["session_status"]({"num": w.num})))
        assert st["reply"] == "改好了"
        assert (await t["send_to_session"]({"num": router.num, "text": "x"}))["isError"]   # 不能派给自己
        assert (await t["send_to_session"]({"num": 999, "text": "x"}))["isError"]

        # 你直接在网页里跟那个对话说话：不回报给总管，照旧直接通知你
        runs.clear(), pushes.clear()
        web = Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text="再改", via="web")
        await b._submit(w, "再改", web)
        await asyncio.sleep(0.2)
        assert [r[0] for r in runs] == [w.num] and [n for n, _ in pushes] == [w.num]

        # 总管转达：又回报一次
        runs.clear(), pushes.clear()
        assert "已交给" in _out(await t["send_to_session"]({"num": w.num, "text": "按 A 来"}))
        await asyncio.sleep(0.2)
        assert [r[:2] for r in runs] == [(w.num, "router"), (router.num, "report")]

    asyncio.run(go())


def test_folder_errors(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    _setup(monkeypatch, [])

    async def go():
        b = Bridge(_cfg(tmp_path), FakeDT(), data_dir=tmp_path / "data")
        _, t = _tools(b)
        assert (await t["new_session"]({"task": "x", "folder": "根本不存在的文件夹zz"}))["isError"]
        assert (await t["new_session"]({"task": " "}))["isError"]
        assert "已开" in _out(await t["new_session"]({"task": "随便查查"}))   # 不写文件夹：临时文件夹
        await asyncio.sleep(0.1)

    asyncio.run(go())


def test_report_text():
    from anzhuo.report import Turn
    from anzhuo.sessions import Session
    s = Session(num=3, name="登录页", cwd="/tmp")
    t = Turn(started=0, request="x", final_text="好了")
    t.changed_files = ["/a/b.py", "/a/b.py", "/a/c.py"]
    assert superagent.report_text(s, t).startswith("【回报】#3 登录页：做完了；改了 b.py、c.py")
    s.awaiting, s.awaiting_kind = "要 A 还是 B？", "decide"
    assert "在等用户定" in superagent.report_text(s, t) and "要 A 还是 B？" in superagent.report_text(s, t)
    t.is_error, t.error = True, "额度用完"
    assert "出错了" in superagent.report_text(s, t)
