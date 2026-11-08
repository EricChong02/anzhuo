"""不连钉钉、不调 Claude，走一遍：开对话、选文件夹、引用回复、列表、报告推送。"""
import asyncio
import sqlite3
from pathlib import Path
import pytest

from anzhuo.app import Bridge
from anzhuo.config import Config
from anzhuo.sessions import Runner


class FakeDT:
    secret = "x"

    def __init__(self):
        self.sent = []
        self.cards = {}

    async def send_markdown(self, uid, title, text):
        self.sent.append((title, text))

    async def send_image(self, uid, path):
        self.sent.append(("图片", str(path)))

    async def create_card(self, uid, oid, data):
        self.cards[oid] = [data]

    async def update_card(self, oid, data):
        self.cards[oid].append(data)

    async def download(self, code, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG")
        return dest


def msg(text, quoted=None):
    d = {"msgtype": "text", "text": {"content": text}, "senderStaffId": "me", "senderNick": "作者", "conversationType": "1"}
    if quoted:
        d["text"]["repliedMsg"] = {"content": {"text": quoted}}
    return d


def test_flow(tmp_path, monkeypatch):
    (tmp_path / "proj" / "菜谱整理").mkdir(parents=True)
    (tmp_path / "proj" / "旧菜谱").mkdir(parents=True)
    db = tmp_path / "lab.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE users (name TEXT, student_id TEXT)")
    con.execute("INSERT INTO users VALUES ('王小明', '20250001')")
    con.commit()
    con.close()
    cfg = Config(client_id="x", allowed_user_ids=["me"], folders={"根": str(tmp_path / "proj")},
                 search_roots=[str(tmp_path)], scratch_root=str(tmp_path / "scratch"),
                 auto_route=False)
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, s.cwd, job.text, len(job.images)))
        turn.changed_files.append(s.cwd + "/x.md")
        turn.final_text = "要你做的：没有\n做了什么：看了王小明的作业"
        s.claude_id = f"sid-{s.num}"

    monkeypatch.setattr(Runner, "_run", fake_run)

    async def no_usage(self, force=False):
        self.limits = {"five_hour": (0.3, "10月25日 17:10")}

    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 0
        await b.ding.on_raw({**msg("你好"), "senderStaffId": "stranger"})
        assert dt.sent == []  # 陌生人不理
        await b.ding.on_raw(msg("新开 菜谱：整理一下"))
        assert "回编号" in dt.sent[-1][1]
        await b.ding.on_raw(msg("2"))
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("新开：随便查查"))
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("再看看", quoted="【做完了】#1 整理一下"))
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("列表"))
        # 先发图、再发话：合成一件活
        await b.ding.on_raw({"msgtype": "picture", "content": {"downloadCode": "c1"}, "senderStaffId": "me",
                        "conversationType": "1"})
        await b.ding.on_raw(msg("这张图里有什么"))
        await asyncio.sleep(0.05)
        # 只发图不说话：等一会儿单独交
        b.ding.photo_wait = 0.01
        await b.ding.on_raw({"msgtype": "picture", "content": {"downloadCode": "c2"}, "senderStaffId": "me",
                        "conversationType": "1"})
        await asyncio.sleep(0.1)
        return b

    b = asyncio.run(go())
    assert seen[0][0] == 1 and seen[0][1].endswith(("旧菜谱", "菜谱整理")) and seen[0][2] == "整理一下"
    assert seen[1][0] == 2 and "scratch" in seen[1][1]
    assert seen[2][0] == 1 and "引用的内容" in seen[2][2]  # 引用 #1 的消息回到 #1
    assert seen[3][2:] == ("这张图里有什么", 1)
    assert seen[4][2:] == ("", 1)
    done = [t for t, _ in dt.sent if t.startswith("做完了")]
    assert len(done) == 5
    listing = next(text for t, text in dt.sent if t == "对话列表")
    assert "#1" in listing and "#2" in listing
    assert b.registry.sessions[1].claude_id == "sid-1"


def test_auto_route(tmp_path, monkeypatch):
    router = pytest.importorskip("anzhuo.router")
    (tmp_path / "课程").mkdir()
    cfg = Config(client_id="x", allowed_user_ids=["me"], folders={"前端": str(tmp_path / "课程")},
                 search_roots=[str(tmp_path)], scratch_root=str(tmp_path / "scratch"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, s.cwd, job.text))
        turn.final_text = "好的"

    async def no_usage(self, force=False):
        pass

    answers = iter([
        router.Route("new", None, "前端"),     # 新事 → 自动在前端新开 #1
        router.Route("ask", None, None),         # 拿不准 → 问
        router.Route("new", None, "根本没有的文件夹"),  # 文件夹找不到 → 问
    ])

    async def fake_route(*a, **k):
        return next(answers)

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(router, "route", fake_route)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 0
        await b.ding.on_raw(msg("把第二课封面标题改大"))
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("再大一点"))  # 一看就是回复：不请远窗判断，直接接当前对话
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("顺便查下天气"))
        assert "回编号" in dt.sent[-1][1]
        await b.ding.on_raw(msg("2"))  # 选新开
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("去那个文件夹看看"))
        assert "回编号" in dt.sent[-1][1]
        await b.ding.on_raw(msg("1"))  # 选接着当前
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert seen[0][:2] == (1, str(tmp_path / "课程"))
    assert seen[1][0] == 1 and seen[1][2] == "再大一点"
    assert seen[2][0] == 2 and "scratch" in seen[2][1]
    assert seen[3][0] == 2 and seen[3][2] == "去那个文件夹看看"


def test_chat_follow_up(tmp_path, monkeypatch):
    """远窗直接回答后，你接着追问（哪怕很短）还交给远窗，并带上刚才那段话。"""
    router = pytest.importorskip("anzhuo.router")
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"))
    calls = []
    answers = iter([
        router.Route("reply", answer="叫“王老师的先生”就好。"),
        router.Route("reply", answer="那就叫“先生”或跟着叫他名字。"),
        router.Route("reply", answer="不客气！"),
    ])

    async def fake_route(text, *a, chat=None, **k):
        calls.append((text, list(chat or [])))
        return next(answers)

    monkeypatch.setattr(router, "route", fake_route)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        await b.ding.on_raw(msg("见到王老师的老公怎么称呼"))
        await b.ding.on_raw(msg("她老公不是老师"))
        await b.ding.on_raw(msg("好的"))  # 平时会直接交给当前对话；刚闲聊过就还给远窗

    asyncio.run(go())
    assert [c[0] for c in calls] == ["见到王老师的老公怎么称呼", "她老公不是老师", "好的"]
    assert calls[0][1] == []
    assert calls[1][1] == [("见到王老师的老公怎么称呼", "叫“王老师的先生”就好。")]
    assert len(calls[2][1]) == 2
    assert all(t == "回复" for t, _ in dt.sent)


def test_hide_and_delete(tmp_path, monkeypatch):
    """7 天没动的自动收起；“删 N”收起；收起的不进列表、不给远窗分派；点名“#N …”找回。"""
    import time
    router = pytest.importorskip("anzhuo.router")
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"))
    seen, offered = [], []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, job.text))
        turn.final_text = "好的"

    async def no_usage(self, force=False):
        pass

    async def fake_route(text, sessions, *a, **k):
        offered.append(sorted(x.num for x in sessions))
        return router.Route("continue", target=3)

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(router, "route", fake_route)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 0
        old = time.time() - 8 * 86400
        for n, upd in [(1, old), (2, time.time()), (3, time.time())]:
            b.registry.add(Session(num=n, name=f"事{n}", cwd=str(tmp_path), updated=upd))
        await b.ding.on_raw(msg("删 2"))
        assert "收起了" in dt.sent[-1][1] and b.registry.current == 3
        await b.ding.on_raw(msg("列表"))
        listing = dt.sent[-1][1]
        assert "#3" in listing and "#1" not in listing and "#2" not in listing and "另有 2 个收起了" in listing
        await b.ding.on_raw(msg("列表 全部"))
        assert "#1" in dt.sent[-1][1] and "（收起）" in dt.sent[-1][1]
        await b.ding.on_raw(msg("帮我看看这一节的封面"))
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("#2 其实还要接着做"))
        await asyncio.sleep(0.05)
        assert not b.registry.sessions[2].hidden

    asyncio.run(go())
    assert offered == [[3]]
    assert seen == [(3, "帮我看看这一节的封面"), (2, "其实还要接着做")]


def test_desktop_resume(tmp_path, monkeypatch):
    """“接着桌面”接上最近的桌面端对话；远窗自己开的、临时文件夹、家目录里的不算；回编号换一段。"""
    from types import SimpleNamespace as NS
    from anzhuo import dingtalk_chat
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"))
    home = str(Path.home())

    def info(sid, cwd, title, t):
        return NS(session_id=sid, cwd=cwd, custom_title=title, summary="", first_prompt="", last_modified=t)

    infos = [
        info("router", home, "分派", 900),
        info("mine", str(tmp_path / "课"), "远窗开的", 800),
        info("scratch", str(tmp_path / "scratch" / "1"), "临时", 700),
        info("desk-a", str(tmp_path / "平台"), "远程开发课程工具", 600),
        info("desk-b", str(tmp_path / "课"), "做第二课", 500),
    ]
    monkeypatch.setattr(dingtalk_chat, "list_sessions", lambda *a: infos)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.registry.add(Session(num=b.registry.reserve_num(), name="远窗开的", cwd=str(tmp_path / "课"), claude_id="mine"))
        await b.ding.on_raw(msg("接着桌面"))
        s = b.registry.get(b.registry.current)
        assert (s.claude_id, s.from_desktop) == ("desk-a", True)
        assert "接上了" in dt.sent[-1][1] and "做第二课" in dt.sent[-1][1]
        await b.ding.on_raw(msg("2"))  # 换成第二段，刚接错的那段收起
        assert b.registry.get(b.registry.current).claude_id == "desk-b"
        assert b.registry.get(2).hidden
        await b.ding.on_raw(msg("接着桌面"))  # 再接同一段：沿用原编号，不重复登记
        assert b.registry.current == 2 and not b.registry.get(2).hidden
        assert len(b.registry.sessions) == 3

    asyncio.run(go())


def test_cards_flow(tmp_path, monkeypatch):
    """做得久的发进度卡、原地更新、结束收尾；“要你定”出按钮卡，点按钮把选择交回对话；陌生人点无效。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"), auto_route=False, dingtalk_quiet=False)
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append(job.text)
        if job.text.startswith("选"):
            turn.final_text = "好的，按 B 做了。"
            return
        for step in ("看 a.py", "改 a.py", "跑命令：测试"):
            turn.steps.append(step)
            await asyncio.sleep(0.03)
        turn.changed_files.append(str(tmp_path / "a.py"))
        turn.final_text = "要你定：标题用哪个？\n\nA. 大\nB. 中\n\n推荐：B"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay, b.dcards.progress_every = 0.01, (0.01, 0.01)
        b.registry.add(Session(num=1, name="封面", cwd=str(tmp_path)))
        await b.ding.on_raw(msg("把封面标题改大"))
        await asyncio.sleep(0.4)
        boards = [v for k, v in dt.cards.items() if k.startswith("b-")]
        assert boards and any("跑命令：测试" in d["markdown"] for d in boards[0])
        assert boards[-1][-1]["title"].endswith("都做完了")  # 做完了就收尾
        oid = next(k for k in dt.cards if k.startswith("d1-"))
        assert "选 B（推荐）" in dt.cards[oid][0]["sys_full_json_obj"]
        await b.dcards.on_click({"userId": "stranger", "outTrackId": oid, "content": '{"cardPrivateData":{"actionIds":["pick:A"]}}'})
        await b.dcards.on_click({"userId": "me", "outTrackId": oid, "content": '{"cardPrivateData":{"actionIds":["pick:B"]}}'})
        await b.dcards.on_click({"userId": "me", "outTrackId": oid, "content": '{"cardPrivateData":{"actionIds":["pick:A"]}}'})
        await asyncio.sleep(0.2)
        assert "你选了 B" in dt.cards[oid][-1]["markdown"]
        b2 = Bridge(cfg, dt, data_dir=tmp_path / "data")  # 重启后还认得这张卡
        assert b2.dcards.store[oid]["picked"] == "B"

    asyncio.run(go())
    assert seen == ["把封面标题改大", "选 B：中"]


def test_reply_forward(tmp_path, monkeypatch):
    """远窗直接回答时附“交给 #N”；点了把原话转过去。带图的话不让远窗答（它看不到图），直接交当前对话。"""
    router = pytest.importorskip("anzhuo.router")
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, job.text, len(job.images)))
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    async def fake_route(*a, **k):
        return router.Route("reply", answer="这是远窗的回答")

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(router, "route", fake_route)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 0
        b.registry.add(Session(num=4, name="开发远窗", cwd=str(tmp_path)))
        await b.ding.on_raw(msg("以后能同时做几个对话吗"))
        oid = next(k for k in dt.cards if k.startswith("r4-"))
        assert "交给 #4" in dt.cards[oid][0]["sys_full_json_obj"]
        await b.dcards.on_click({"userId": "me", "outTrackId": oid, "content": '{"cardPrivateData":{"actionIds":["fwd"]}}'})
        await asyncio.sleep(0.05)
        assert "已转给" in dt.cards[oid][-1]["tips"]
        pic = {"msgtype": "picture", "content": {"downloadCode": "c1"}, "senderStaffId": "me", "conversationType": "1"}
        await b.ding.on_raw(pic)
        await b.ding.on_raw(msg("按了之后好像没啥反应"))
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert seen == [(4, "以后能同时做几个对话吗", 0), (4, "按了之后好像没啥反应", 1)]


def test_recent_record(tmp_path):
    """来往原文只留最近 100 行。"""
    import json as _json
    from anzhuo import app as app_mod
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False)
    b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")
    for i in range(app_mod.RECENT_KEEP + 20):
        b.record("你", f"第{i}句")
    rows = [_json.loads(x) for x in (tmp_path / "data" / "recent.jsonl").read_text().splitlines()]
    assert len(rows) == app_mod.RECENT_KEEP and rows[-1]["text"] == f"第{app_mod.RECENT_KEEP + 19}句"


def test_folder_blocked(tmp_path, monkeypatch):
    """文件夹打不开（macOS 在等授权）时不去启动 Claude，直接说清原因。"""
    from anzhuo import folders as folders_mod
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False)
    monkeypatch.setattr(folders_mod, "readable", lambda p, timeout=5: False)

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 5
        b.registry.add(Session(num=1, name="清理", cwd=str(tmp_path)))
        await b.ding.on_raw(msg("清理一下"))
        await asyncio.sleep(0.2)

    asyncio.run(go())
    title, body = dt.sent[-1]
    assert title.startswith("出错了") and "隐私保护" in body


def test_unsent_carried_over(tmp_path, monkeypatch):
    """第一句没能开始（文件夹打不开），下一句会带上它。"""
    from anzhuo import folders as folders_mod
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False)
    ok = {"v": False}
    monkeypatch.setattr(folders_mod, "readable", lambda p, timeout=5: ok["v"])
    seen = []

    async def fake_run(self, s, job, turn):
        if not ok["v"]:
            raise RuntimeError("不该到这")
        seen.append(job.text)
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    real_run = Runner._run

    async def run(self, s, job, turn):
        if not folders_mod.readable(Path(s.cwd)):
            return await real_run(self, s, job, turn)
        return await fake_run(self, s, job, turn)

    monkeypatch.setattr(Runner, "_run", run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 5
        b.registry.add(Session(num=1, name="清理", cwd=str(tmp_path)))
        await b.ding.on_raw(msg("清理下载文件夹"))
        await asyncio.sleep(0.1)
        ok["v"] = True
        await b.ding.on_raw(msg("权限开好了"))
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert seen == ["清理下载文件夹\n\n权限开好了"]


def test_board_follows(tmp_path, monkeypatch):
    """两件同时做：一件做完发报告后，工作台挪到报告下面、旧的缩成一行；点“停 #N”停对应那件；都完了收尾。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False, dingtalk_quiet=False)
    gate = asyncio.Event()

    async def fake_run(self, s, job, turn):
        turn.steps.append("看 a.py")
        if s.num == 2:
            await gate.wait()
        turn.changed_files.append(str(tmp_path / "a.py"))
        turn.final_text = "做完了"

    async def no_usage(self, force=False):
        pass

    stopped = []

    async def fake_stop(self, num):
        stopped.append(num)
        gate.set()
        return True

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(Runner, "stop", fake_stop)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay, b.dcards.progress_every = 0.05, (0.02, 0.02)
        b.registry.add(Session(num=1, name="甲", cwd=str(tmp_path)))
        b.registry.add(Session(num=2, name="乙", cwd=str(tmp_path)))
        await b.ding.on_raw(msg("#2 做乙"))
        await asyncio.sleep(0.15)
        first = b.dcards.board
        assert first and "#2 乙" in dt.cards[first][-1]["markdown"]
        await b.ding.on_raw(msg("#1 做甲"))  # 甲很快做完 → 报告 → 工作台挪下来
        await asyncio.sleep(0.15)
        assert b.dcards.board != first and dt.cards[first][-1]["markdown"].startswith("进展挪到下面了")
        await b.dcards.on_click({"userId": "me", "outTrackId": b.dcards.board, "content": '{"cardPrivateData":{"params":{"id":"stop:2"}}}'})
        await asyncio.sleep(0.2)
        assert stopped == [2] and b.dcards.board is None

    asyncio.run(go())


def test_inject_while_running(tmp_path, monkeypatch):
    """对话正在做时你再说话：直接插给它（不排队），回一句“插话给 #N 了”。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False, dingtalk_quiet=False)
    gate, got = asyncio.Event(), []

    class FakeClient:
        async def query(self, prompt):
            async for m in prompt:
                got.append(m["message"]["content"][-1]["text"])
            gate.set()

    async def fake_run(self, s, job, turn):
        self.live[s.num] = FakeClient()
        await gate.wait()
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 5
        b.registry.add(Session(num=1, name="开发", cwd=str(tmp_path)))
        await b.ding.on_raw(msg("把按钮做大"))
        await asyncio.sleep(0.05)
        await b.ding.on_raw(msg("顺便把颜色改成蓝色"))
        await asyncio.sleep(0.1)
        assert any(t == "插话" for t, _ in dt.sent)

    asyncio.run(go())
    assert got == ["顺便把颜色改成蓝色"]


def test_choice_goes_to_waiting_session(tmp_path, monkeypatch):
    """#6 列了 A/B/C 在等你回答，当前对话却是 #4：你说“按C来做”应该交给 #6。"""
    from anzhuo.intent import asks_user
    from anzhuo.sessions import Session
    six = ("有空间。下面是几个方案。\n\n| 方案 | 改动 |\n|---|---|\n| A. 收窄面板 | 320 → 260 |\n"
           "| B. 收起面板 | 点一下隐藏 |\n| C. 全屏预览 | 铺满 |\n\n我推荐 **C**。\n\n要我按 C 来做吗？")
    assert asks_user(six) == "要我按 C 来做吗？"
    assert asks_user("改好了，跑了测试，都通过。") == ""
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, job.text))
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    async def no_router(*a, **k):
        raise AssertionError("这种回复不该再请远窗判断")

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    from anzhuo import app
    if app.router:   # 开源导出没有分派员，本来就不会去判断
        monkeypatch.setattr(app.router, "route", no_router)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 5
        b.registry.add(Session(num=6, name="做课", cwd=str(tmp_path), awaiting="要我按 C 来做吗？"))
        b.registry.add(Session(num=4, name="开发", cwd=str(tmp_path)))  # 后加的，是当前对话
        await b.ding.on_raw(msg("按C来做"))
        await asyncio.sleep(0.05)
        assert b.registry.get(6).awaiting == ""

    asyncio.run(go())
    assert seen == [(6, "按C来做")]


def test_compact(tmp_path, monkeypatch):
    """“压缩”：正在做就排到这一轮之后（不插话），压完报上下文从多少到多少。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False, dingtalk_quiet=False)
    gate, seen = asyncio.Event(), []

    async def fake_run(self, s, job, turn):
        seen.append(job.text)
        if job.text == "/compact":
            s.context_pct = 12
            return
        await gate.wait()
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 5
        b.registry.add(Session(num=6, name="做课", cwd=str(tmp_path), claude_id="sid", context_pct=59))
        await b.ding.on_raw(msg("改封面"))
        await asyncio.sleep(0.05)
        b.runner.live[6] = object()       # 假装正在做、能插话
        await b.ding.on_raw(msg("压缩"))
        assert "等手上 1 件做完再压" in dt.sent[-1][1]
        gate.set()
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert seen == ["改封面", "/compact"]
    assert any(t == "压缩好了" and "59% → 12%" in x for t, x in dt.sent)



def test_quiet_dingtalk(tmp_path, monkeypatch):
    """钉钉安静模式（默认）：开工、收到、插话、工作台都不发，只发做完的报告。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False)
    gate = asyncio.Event()

    class FakeClient:
        async def query(self, prompt):
            async for _ in prompt:
                pass

    async def fake_run(self, s, job, turn):
        self.live[s.num] = FakeClient()
        turn.steps.append("看 a.py")
        await gate.wait()
        turn.changed_files.append(str(tmp_path / "a.py"))
        turn.final_text = "改好了"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 0.01
        await b.ding.on_raw(msg("新开 根：做一件要很久的事"))      # 文件夹找不到也会回话；改用临时文件夹
        await b.ding.on_raw(msg("新开：做一件要很久的事"))
        await asyncio.sleep(0.1)
        await b.ding.on_raw(msg("顺便看看别的"))                    # 正在做 → 插话
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.2)

    asyncio.run(go())
    titles = [t for t, _ in dt.sent]
    assert dt.cards == {}                                          # 没发工作台
    assert not any(t in ("收到", "插话") for t in titles)          # 没发开工、收到、插话
    assert any(t.startswith("做完了") for t in titles)              # 做完的报告照发


def test_start_keeps_picked_model(tmp_path):
    """网页上选的模型真的用上：选 Sonnet 就是 Sonnet，选 GPT 就是 Codex；没选按任务类型挑。（真 Bridge，不是假的）"""
    from anzhuo.dingtalk import Incoming
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False)

    async def go():
        b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")
        b.runner.submit = lambda s, job: 0          # 不真跑
        inc = Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text="x")
        await b.new_session([], "写首诗", inc, model="claude-sonnet-5-5", effort="high")
        await b.new_session([], "聊聊天", inc, model="gpt")
        await b.new_session([], "随便问问", inc)
        ss = sorted(b.registry.sessions.values(), key=lambda s: s.num)
        assert (ss[0].engine, ss[0].model, ss[0].effort) == ("claude", "claude-sonnet-5-5", "high")
        assert (ss[1].engine, ss[1].model) == ("codex", None)
        assert ss[2].engine == "claude" and ss[2].model

    asyncio.run(go())


def test_notify_routing(tmp_path, monkeypatch):
    """做完怎么通知：正开着看 → 什么都不发；手机通知发到了 → 钉钉不说；没发到 → 钉钉一句话带链接；钉钉里说的 → 照旧全发回钉钉。"""
    from anzhuo.dingtalk import Incoming
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False,
                 web_base="https://xe.example")

    async def fake_run(self, s, job, turn):
        turn.changed_files.append(str(tmp_path / "a.py"))
        turn.final_text = "改好了"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()
    pushes = []

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        ok = {"n": 1}

        async def fake_push(title, body, num=None, only=None):
            pushes.append(body)
            return ok["n"]
        b.web.push.send = fake_push
        web = lambda t: Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text=t, via="web")
        await b.new_session([], "查一下", web("查一下"))
        await asyncio.sleep(0.2)
        assert len(pushes) == 1 and dt.sent == []                       # 推送到了：钉钉安静
        s = b.registry.get(1)
        b.web.watch[1] = __import__("time").time()                     # 正开着看
        await b.submit(s, "再查", web("再查"))
        await asyncio.sleep(0.2)
        assert len(pushes) == 1 and dt.sent == []
        b.web.watch.clear()
        ok["n"] = 0                                                     # 推送没发到
        await b.submit(s, "又查", web("又查"))
        await asyncio.sleep(0.2)
        assert len(dt.sent) == 1 and "在远窗看" in dt.sent[0][1] and "/_x/#s1" in dt.sent[0][1]
        dt.sent.clear()
        await b.ding.on_raw(msg("#1 钉钉里问"))                              # 钉钉里说的：完整报告回钉钉
        await asyncio.sleep(0.2)
        assert any("改好了" in t for _, t in dt.sent)

    asyncio.run(go())


def test_limit_auto_resume(tmp_path, monkeypatch):
    """撞上 5 小时额度：记下几点恢复；到点后自动说“继续”；点了“别自动接”就不说。"""
    import time
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False)
    runs = []

    async def fake_run(self, s, job, turn):
        runs.append(job.text)
        if len(runs) == 1:
            turn.is_error, turn.error = True, "You've hit your session limit · resets 5:40pm (Asia/Taipei)"
        else:
            turn.final_text = "接着做完了"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    from anzhuo import app as appmod
    monkeypatch.setattr(appmod, "limit_reset", lambda text, now=None: time.time() - 59.9 if "limit" in text else None)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        await b.ding.on_raw(msg("新开：做件事"))
        await asyncio.sleep(0.5)
        assert runs == ["做件事", "继续"] and b.registry.get(1).limit_until == 0

    asyncio.run(go())


@__import__("pytest").mark.parametrize("engine", ["claude", "codex"])
def test_relay(tmp_path, monkeypatch, engine):
    """接力：长对话先写交接说明，写完开一个同文件夹、同模型的新对话；交接说明随你第一句话交给新对话。"""
    from anzhuo.relay import RELAY
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), auto_route=False)
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, job.text))
        if job.text == RELAY:
            turn.final_text = "## 目标\n做封面"
        else:
            s.claude_id = "new"
            turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(Runner, "refresh_gpt_usage", no_usage)   # Codex 的额度要真开 app-server 去读，测试里不读

    async def go():
        b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")
        b.registry.add(Session(num=6, name="做课封面和目录", cwd=str(tmp_path), engine=engine, claude_id="sid" if engine == "claude" else None,
                               codex_id="cid" if engine == "codex" else None, model="test-model",
                               effort="high", context_tokens=300_000))
        from anzhuo.sessions import Job
        from anzhuo.report import Turn
        old = b.registry.get(6)
        old.context_tokens = 1000
        assert "没有接力建议" in await b.relays.start(6)
        old.context_tokens = 300_000
        b.runner.turns[6] = Turn(started=1)
        assert "等回答结束" in await b.relays.start(6) and 6 not in b.relays.writing
        b.runner.turns.pop(6)
        b.runner.queues[6] = asyncio.Queue()
        b.runner.queues[6].put_nowait(Job(text="排队消息"))
        assert "等回答结束" in await b.relays.start(6) and 6 not in b.relays.writing
        b.runner.queues[6].get_nowait()
        old.awaiting, old.awaiting_kind = "选哪一个", "decide"
        b.runner.last[6] = Turn(started=1, request="设计", final_text="要你定：选哪一个？\nA. 红色\nB. 蓝色\n推荐：A，醒目")
        assert "交接说明" in await b.relays.start(6)
        assert "正在写" in await b.relays.start(6)              # 点两次只写一份
        await asyncio.sleep(0.2)
        old = b.registry.get(6)
        new = b.registry.get(old.relay_to)
        assert new and new.relay_from == 6 and new.cwd == old.cwd and new.model == old.model and new.effort == "high"
        assert new.relay_note == "## 目标\n做封面" and new.claude_id is None and new.codex_id is None and new.engine == engine
        assert new.awaiting == "选哪一个" and new.awaiting_kind == "decide"
        assert "A. 红色" in new.unsent and "不宣告接手" in new.unsent
        assert "已经接力到" in await b.relays.start(6)
        b.runner.submit(new, __import__("anzhuo.sessions", fromlist=["Job"]).Job(text="接着做"))
        await asyncio.sleep(0.2)
        return new.num

    n = asyncio.run(go())
    assert seen[0] == (6, RELAY) and len(seen) == 2
    assert seen[1][0] == n and "## 目标\n做封面" in seen[1][1] and seen[1][1].endswith("用户这次说：\n\n接着做")


def test_relay_hint():
    """什么时候提示接力：上下文 12 万以上；或隔了一个多小时回来、上下文 8 万以上（缓存过期要整段重写）。门槛可配置。"""
    from anzhuo.config import Config
    from anzhuo.sessions import Session
    from anzhuo.relay import relay_hint
    now = 1_000_000.0
    s = Session(num=1, name="x", cwd="/", claude_id="a", context_tokens=130_000, updated=now - 60)
    assert relay_hint(s, False, False, now)["kind"] == "long"
    assert relay_hint(s, False, False, now, cfg=Config(relay_long=200_000)) is None   # 门槛调高了就不提示
    s.context_tokens = 250_000
    assert relay_hint(s, True, False, now) is None                 # 正在做不打扰
    assert relay_hint(s, False, True, now) == {"kind": "running"}
    s.updated = now - 2 * 3600
    h = relay_hint(s, False, False, now)
    assert h["kind"] == "cold" and h["hours"] == 2.0
    s.context_tokens = 50_000
    assert relay_hint(s, False, False, now) is None                # 短对话重写也不贵
    s.context_tokens, s.relay_to = 250_000, 9
    assert relay_hint(s, False, False, now) is None                # 已经接力过


def test_relay_hint_compact_when_window_nearly_full():
    """上下文用到七成：提示先压缩（活没做完）、做完再接力；1M 窗口的对话 12 万时还是只问“这一段做完了吗”。"""
    from anzhuo.relay import RELAY, relay_hint
    from anzhuo.sessions import Session
    now = 1_000_000.0
    s = Session(num=1, name="x", cwd="/", claude_id="a", context_tokens=150_000, context_pct=75.0, updated=now - 60)
    h = relay_hint(s, False, False, now)
    assert h["kind"] == "compact" and h["pct"] == 75 and h["key"] == "compact7"
    s.context_pct = 15.0
    assert relay_hint(s, False, False, now)["kind"] == "long"
    assert "2000 字" not in RELAY and "宁可长一点" in RELAY   # 交接说明不硬限字数


def test_new_session_model_by_kind(tmp_path, monkeypatch):
    """新开对话没指定模型：按分派员判断的档位照分工表挑；定方案用 design 档，判断不出来时代码仓按开发算。"""
    from anzhuo import split
    router = pytest.importorskip("anzhuo.router")
    from anzhuo.engines import MODELS
    from anzhuo.dingtalk import Incoming
    (tmp_path / "proj" / ".git").mkdir(parents=True)
    (tmp_path / "notes").mkdir()
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"),
                 split=split.normalize({"dev": {"use": "haiku"}}))
    said = {}

    async def classify(text, timeout=15):
        return said.get(text)
    monkeypatch.setattr(router, "classify_kind", classify)

    async def fake_run(self, s, job, turn):
        turn.final_text = "好"
    monkeypatch.setattr(Runner, "_run", fake_run)
    inc = lambda t: Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text=t, via="web")

    async def go():
        b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")
        assert b.runner.subagent_model == MODELS["sonnet"]   # 子代理默认 Sonnet
        said["设计一节课"] = "design"
        out = []
        for cwd, text in [(tmp_path / "notes", "设计一节课"), (tmp_path / "proj", "修个 bug"), (tmp_path / "notes", "查个资料"),
                          (tmp_path / "notes", "设计一节课 用 sonnet")]:
            n = b.registry.reserve_num()
            await b.start_session(n, cwd, [], text, inc(text))
            out.append(b.registry.get(n).model)
        return out

    assert asyncio.run(go()) == [MODELS["opus"], MODELS["haiku"], MODELS["sonnet"], MODELS["sonnet"]]
