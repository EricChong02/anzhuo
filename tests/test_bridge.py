"""不连钉钉、不调 Claude，走一遍：开对话、选文件夹、引用回复、列表、报告推送。"""
import asyncio
import sqlite3
from pathlib import Path

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
    d = {"msgtype": "text", "text": {"content": text}, "senderStaffId": "me", "senderNick": "测试", "conversationType": "1"}
    if quoted:
        d["text"]["repliedMsg"] = {"content": {"text": quoted}}
    return d


def test_flow(tmp_path, monkeypatch):
    (tmp_path / "proj" / "菜谱整理").mkdir(parents=True)
    (tmp_path / "proj" / "旧菜谱").mkdir(parents=True)
    cfg = Config(client_id="x", allowed_user_ids=["me"], folders={"根": str(tmp_path / "proj")},
                 search_roots=[str(tmp_path)], scratch_root=str(tmp_path / "scratch"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, s.cwd, job.text, len(job.images)))
        turn.changed_files.append(s.cwd + "/x.md")
        turn.final_text = "要你做的：没有\n做了什么：看了代码"
        s.claude_id = f"sid-{s.num}"

    monkeypatch.setattr(Runner, "_run", fake_run)

    async def no_usage(self, force=False):
        self.limits = {"five_hour": (0.3, "10月25日 17:10")}

    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 0
        await b.on_raw({**msg("你好"), "senderStaffId": "stranger"})
        assert dt.sent == []  # 陌生人不理
        await b.on_raw(msg("新开 菜谱：整理一下"))
        assert "回编号" in dt.sent[-1][1]
        await b.on_raw(msg("2"))
        await asyncio.sleep(0.05)
        await b.on_raw(msg("新开：随便查查"))
        await asyncio.sleep(0.05)
        await b.on_raw(msg("再看看", quoted="【做完了】#1 整理一下"))
        await asyncio.sleep(0.05)
        await b.on_raw(msg("列表"))
        # 先发图、再发话：合成一件活
        await b.on_raw({"msgtype": "picture", "content": {"downloadCode": "c1"}, "senderStaffId": "me",
                        "conversationType": "1"})
        await b.on_raw(msg("这张图里有什么"))
        await asyncio.sleep(0.05)
        # 只发图不说话：等一会儿单独交
        b.photo_wait = 0.01
        await b.on_raw({"msgtype": "picture", "content": {"downloadCode": "c2"}, "senderStaffId": "me",
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


def test_hide_and_delete(tmp_path, monkeypatch):
    """7 天没动的自动收起；“删 N”收起；收起的不进列表、不再是当前对话；点名“#N …”找回。"""
    import time
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, job.text))
        turn.final_text = "好的"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 0
        old = time.time() - 8 * 86400
        for n, upd in [(1, old), (2, time.time()), (3, time.time())]:
            b.registry.add(Session(num=n, name=f"事{n}", cwd=str(tmp_path), updated=upd))
        await b.on_raw(msg("删 2"))
        assert "收起了" in dt.sent[-1][1] and b.registry.current == 3
        await b.on_raw(msg("列表"))
        listing = dt.sent[-1][1]
        assert "#3" in listing and "#1" not in listing and "#2" not in listing and "另有 2 个收起了" in listing
        await b.on_raw(msg("列表 全部"))
        assert "#1" in dt.sent[-1][1] and "（收起）" in dt.sent[-1][1]
        await b.on_raw(msg("帮我看看这一节的封面"))
        await asyncio.sleep(0.05)
        await b.on_raw(msg("#2 其实还要接着做"))
        await asyncio.sleep(0.05)
        assert not b.registry.sessions[2].hidden

    asyncio.run(go())
    assert seen == [(3, "帮我看看这一节的封面"), (2, "其实还要接着做")]


def test_desktop_resume(tmp_path, monkeypatch):
    """“接着桌面”接上最近的桌面端对话；远窗自己开的、临时文件夹、家目录里的不算；回编号换一段。"""
    from types import SimpleNamespace as NS
    from anzhuo import app as app_mod
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"))
    home = str(Path.home())

    def info(sid, cwd, title, t):
        return NS(session_id=sid, cwd=cwd, custom_title=title, summary="", first_prompt="", last_modified=t)

    infos = [
        info("router", home, "分派", 900),
        info("mine", str(tmp_path / "课"), "远窗开的", 800),
        info("scratch", str(tmp_path / "scratch" / "1"), "临时", 700),
        info("desk-a", str(tmp_path / "后端"), "远程开发工具", 600),
        info("desk-b", str(tmp_path / "课"), "做第二课", 500),
    ]
    monkeypatch.setattr(app_mod, "list_sessions", lambda *a: infos)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.registry.add(Session(num=b.registry.reserve_num(), name="远窗开的", cwd=str(tmp_path / "课"), claude_id="mine"))
        await b.on_raw(msg("接着桌面"))
        s = b.registry.get(b.registry.current)
        assert (s.claude_id, s.from_desktop) == ("desk-a", True)
        assert "接上了" in dt.sent[-1][1] and "做第二课" in dt.sent[-1][1]
        await b.on_raw(msg("2"))  # 换成第二段，刚接错的那段收起
        assert b.registry.get(b.registry.current).claude_id == "desk-b"
        assert b.registry.get(2).hidden
        await b.on_raw(msg("接着桌面"))  # 再接同一段：沿用原编号，不重复登记
        assert b.registry.current == 2 and not b.registry.get(2).hidden
        assert len(b.registry.sessions) == 3

    asyncio.run(go())


def test_cards_flow(tmp_path, monkeypatch):
    """做得久的发进度卡、原地更新、结束收尾；“要你定”出按钮卡，点按钮把选择交回对话；陌生人点无效。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"), dingtalk_quiet=False)
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
        b.receipt_delay, b.progress_every = 0.01, (0.01, 0.01)
        b.registry.add(Session(num=1, name="封面", cwd=str(tmp_path)))
        await b.on_raw(msg("把封面标题改大"))
        await asyncio.sleep(0.4)
        boards = [v for k, v in dt.cards.items() if k.startswith("b-")]
        assert boards and any("跑命令：测试" in d["markdown"] for d in boards[0])
        assert boards[-1][-1]["title"].endswith("都做完了")  # 做完了就收尾
        oid = next(k for k in dt.cards if k.startswith("d1-"))
        assert "选 B（推荐）" in dt.cards[oid][0]["sys_full_json_obj"]
        await b.on_card({"userId": "stranger", "outTrackId": oid, "content": '{"cardPrivateData":{"actionIds":["pick:A"]}}'})
        await b.on_card({"userId": "me", "outTrackId": oid, "content": '{"cardPrivateData":{"actionIds":["pick:B"]}}'})
        await b.on_card({"userId": "me", "outTrackId": oid, "content": '{"cardPrivateData":{"actionIds":["pick:A"]}}'})
        await asyncio.sleep(0.2)
        assert "你选了 B" in dt.cards[oid][-1]["markdown"]
        b2 = Bridge(cfg, dt, data_dir=tmp_path / "data")  # 重启后还认得这张卡
        assert b2.cards[oid]["picked"] == "B"

    asyncio.run(go())
    assert seen == ["把封面标题改大", "选 B：中"]


def test_recent_record(tmp_path):
    """来往原文只留最近 100 行。"""
    import json as _json
    from anzhuo import app as app_mod
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
    b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")
    for i in range(app_mod.RECENT_KEEP + 20):
        b.record("你", f"第{i}句")
    rows = [_json.loads(x) for x in (tmp_path / "data" / "recent.jsonl").read_text().splitlines()]
    assert len(rows) == app_mod.RECENT_KEEP and rows[-1]["text"] == f"第{app_mod.RECENT_KEEP + 19}句"


def test_folder_blocked(tmp_path, monkeypatch):
    """文件夹打不开（macOS 在等授权）时不去启动 Claude，直接说清原因。"""
    from anzhuo import folders as folders_mod
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
    monkeypatch.setattr(folders_mod, "readable", lambda p, timeout=5: False)

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 5
        b.registry.add(Session(num=1, name="清理", cwd=str(tmp_path)))
        await b.on_raw(msg("清理一下"))
        await asyncio.sleep(0.2)

    asyncio.run(go())
    title, body = dt.sent[-1]
    assert title.startswith("出错了") and "隐私保护" in body


def test_unsent_carried_over(tmp_path, monkeypatch):
    """第一句没能开始（文件夹打不开），下一句会带上它。"""
    from anzhuo import folders as folders_mod
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
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
        await b.on_raw(msg("清理下载文件夹"))
        await asyncio.sleep(0.1)
        ok["v"] = True
        await b.on_raw(msg("权限开好了"))
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert seen == ["清理下载文件夹\n\n权限开好了"]


def test_board_follows(tmp_path, monkeypatch):
    """两件同时做：一件做完发报告后，工作台挪到报告下面、旧的缩成一行；点“停 #N”停对应那件；都完了收尾。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), dingtalk_quiet=False)
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
        b.receipt_delay, b.progress_every = 0.05, (0.02, 0.02)
        b.registry.add(Session(num=1, name="甲", cwd=str(tmp_path)))
        b.registry.add(Session(num=2, name="乙", cwd=str(tmp_path)))
        await b.on_raw(msg("#2 做乙"))
        await asyncio.sleep(0.15)
        first = b.board
        assert first and "#2 乙" in dt.cards[first][-1]["markdown"]
        await b.on_raw(msg("#1 做甲"))  # 甲很快做完 → 报告 → 工作台挪下来
        await asyncio.sleep(0.15)
        assert b.board != first and dt.cards[first][-1]["markdown"].startswith("进展挪到下面了")
        await b.on_card({"userId": "me", "outTrackId": b.board, "content": '{"cardPrivateData":{"params":{"id":"stop:2"}}}'})
        await asyncio.sleep(0.2)
        assert stopped == [2] and b.board is None

    asyncio.run(go())


def test_inject_while_running(tmp_path, monkeypatch):
    """对话正在做时你再说话：直接插给它（不排队），回一句“插话给 #N 了”。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), dingtalk_quiet=False)
    gate, got = asyncio.Event(), []

    class FakeClient:
        async def query(self, prompt):
            async for m in prompt:
                got.append(m["message"]["content"][-1]["text"])
            gate.set()

    async def fake_run(self, s, job, turn):
        self.clients[s.num] = FakeClient()
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
        await b.on_raw(msg("把按钮做大"))
        await asyncio.sleep(0.05)
        await b.on_raw(msg("顺便把颜色改成蓝色"))
        await asyncio.sleep(0.1)
        assert any(t == "插话" for t, _ in dt.sent)

    asyncio.run(go())
    assert got == ["顺便把颜色改成蓝色"]


def test_choice_goes_to_waiting_session(tmp_path, monkeypatch):
    """#6 列了 A/B/C 在等你回答，当前对话却是 #4：你说“按C来做”应该交给 #6。"""
    from anzhuo.app import asks_user
    from anzhuo.sessions import Session
    six = ("有空间。下面是几个方案。\n\n| 方案 | 改动 |\n|---|---|\n| A. 收窄面板 | 320 → 260 |\n"
           "| B. 收起面板 | 点一下隐藏 |\n| C. 全屏试课 | 铺满 |\n\n我推荐 **C**。\n\n要我按 C 来做吗？")
    assert asks_user(six) == "要我按 C 来做吗？"
    assert asks_user("改好了，跑了测试，都通过。") == ""
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, job.text))
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    dt = FakeDT()

    async def go():
        b = Bridge(cfg, dt, data_dir=tmp_path / "data")
        b.receipt_delay = 5
        b.registry.add(Session(num=6, name="做课", cwd=str(tmp_path), awaiting="要我按 C 来做吗？"))
        b.registry.add(Session(num=4, name="开发", cwd=str(tmp_path)))  # 后加的，是当前对话
        await b.on_raw(msg("按C来做"))
        await asyncio.sleep(0.05)
        assert b.registry.get(6).awaiting == ""

    asyncio.run(go())
    assert seen == [(6, "按C来做")]


def test_compact(tmp_path, monkeypatch):
    """“压缩”：正在做就排到这一轮之后（不插话），压完报上下文从多少到多少。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), dingtalk_quiet=False)
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
        await b.on_raw(msg("改封面"))
        await asyncio.sleep(0.05)
        b.runner.clients[6] = object()       # 假装正在做、能插话
        await b.on_raw(msg("压缩"))
        assert "等手上 1 件做完再压" in dt.sent[-1][1]
        gate.set()
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert seen == ["改封面", "/compact"]
    assert any(t == "压缩好了" and "59% → 12%" in x for t, x in dt.sent)



def test_quiet_dingtalk(tmp_path, monkeypatch):
    """钉钉安静模式（默认）：开工、收到、插话、工作台都不发，只发做完的报告。"""
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
    gate = asyncio.Event()

    class FakeClient:
        async def query(self, prompt):
            async for _ in prompt:
                pass

    async def fake_run(self, s, job, turn):
        self.clients[s.num] = FakeClient()
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
        await b.on_raw(msg("新开 根：做一件要很久的事"))      # 文件夹找不到也会回话；改用临时文件夹
        await b.on_raw(msg("新开：做一件要很久的事"))
        await asyncio.sleep(0.1)
        await b.on_raw(msg("顺便看看别的"))                    # 正在做 → 插话
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
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))

    async def go():
        b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")
        b.runner.submit = lambda s, job: 0          # 不真跑
        inc = Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text="x")
        await b._new([], "写首诗", inc, model="claude-sonnet-5-5", effort="high")
        await b._new([], "聊聊天", inc, model="gpt")
        await b._new([], "随便问问", inc)
        ss = sorted(b.registry.sessions.values(), key=lambda s: s.num)
        assert (ss[0].engine, ss[0].model, ss[0].effort) == ("claude", "claude-sonnet-5-5", "high")
        assert (ss[1].engine, ss[1].model) == ("codex", None)
        assert ss[2].engine == "claude" and ss[2].model

    asyncio.run(go())


def test_notify_routing(tmp_path, monkeypatch):
    """做完怎么通知：正开着看 → 什么都不发；手机通知发到了 → 钉钉不说；没发到 → 钉钉一句话带链接；钉钉里说的 → 照旧全发回钉钉。"""
    from anzhuo.dingtalk import Incoming
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"),
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
        await b._new([], "查一下", web("查一下"))
        await asyncio.sleep(0.2)
        assert len(pushes) == 1 and dt.sent == []                       # 推送到了：钉钉安静
        s = b.registry.get(1)
        b.web.watch[1] = __import__("time").time()                     # 正开着看
        await b._submit(s, "再查", web("再查"))
        await asyncio.sleep(0.2)
        assert len(pushes) == 1 and dt.sent == []
        b.web.watch.clear()
        ok["n"] = 0                                                     # 推送没发到
        await b._submit(s, "又查", web("又查"))
        await asyncio.sleep(0.2)
        assert len(dt.sent) == 1 and "在远窗看" in dt.sent[0][1] and "/_x/#s1" in dt.sent[0][1]
        dt.sent.clear()
        await b.on_raw(msg("#1 钉钉里问"))                              # 钉钉里说的：完整报告回钉钉
        await asyncio.sleep(0.2)
        assert any("改好了" in t for _, t in dt.sent)

    asyncio.run(go())


def test_limit_auto_resume(tmp_path, monkeypatch):
    """撞上 5 小时额度：记下几点恢复；到点后自动说“继续”；点了“别自动接”就不说。"""
    import time
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
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
        await b.on_raw(msg("新开：做件事"))
        await asyncio.sleep(0.5)
        assert runs == ["做件事", "继续"] and b.registry.get(1).limit_until == 0

    asyncio.run(go())


@__import__("pytest").mark.parametrize("engine", ["claude", "codex"])
def test_relay(tmp_path, monkeypatch, engine):
    """接力：长对话先写交接说明，写完开一个同文件夹、同模型的新对话；交接说明随你第一句话交给新对话。"""
    from anzhuo.app import RELAY
    from anzhuo.sessions import Session
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"))
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

    async def go():
        b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")
        b.registry.add(Session(num=6, name="做课封面和目录", cwd=str(tmp_path), engine=engine, claude_id="sid" if engine == "claude" else None, codex_id="cid" if engine == "codex" else None, model="test-model",
                               effort="high", context_tokens=300_000))
        from anzhuo.sessions import Job
        from anzhuo.report import Turn
        old = b.registry.get(6)
        old.context_tokens = 1000
        assert "没有接力建议" in b.relay(6)
        old.context_tokens = 300_000
        b.runner.turns[6] = Turn(started=1)
        assert "等回答结束" in b.relay(6) and 6 not in b._relaying
        b.runner.turns.pop(6)
        b.runner.queues[6] = asyncio.Queue()
        b.runner.queues[6].put_nowait(Job(text="排队消息"))
        assert "等回答结束" in b.relay(6) and 6 not in b._relaying
        b.runner.queues[6].get_nowait()
        assert "交接说明" in b.relay(6)
        assert "正在写" in b.relay(6)              # 点两次只写一份
        await asyncio.sleep(0.2)
        old = b.registry.get(6)
        new = b.registry.get(old.relay_to)
        assert new and new.relay_from == 6 and new.cwd == old.cwd and new.model == old.model and new.effort == "high"
        assert new.relay_note == "## 目标\n做封面" and new.claude_id is None and new.codex_id is None and new.engine == engine
        assert "已经接力到" in b.relay(6)
        b.runner.submit(new, __import__("anzhuo.sessions", fromlist=["Job"]).Job(text="接着做"))
        await asyncio.sleep(0.2)
        return new.num

    n = asyncio.run(go())
    assert seen[0] == (6, RELAY) and len(seen) == 2
    assert seen[1][0] == n and "## 目标\n做封面" in seen[1][1] and seen[1][1].endswith("用户这次说：\n\n接着做")


def test_relay_hint():
    """什么时候提示接力：上下文 20 万以上；或隔了一个多小时回来、上下文 8 万以上（缓存过期要整段重写）。"""
    from anzhuo.sessions import Session
    from anzhuo.web import relay_hint
    now = 1_000_000.0
    s = Session(num=1, name="x", cwd="/", claude_id="a", context_tokens=250_000, updated=now - 60)
    assert relay_hint(s, False, False, now)["kind"] == "long"
    assert relay_hint(s, True, False, now) is None                 # 正在做不打扰
    assert relay_hint(s, False, True, now) == {"kind": "running"}
    s.updated = now - 2 * 3600
    h = relay_hint(s, False, False, now)
    assert h["kind"] == "cold" and h["hours"] == 2.0
    s.context_tokens = 50_000
    assert relay_hint(s, False, False, now) is None                # 短对话重写也不贵
    s.context_tokens, s.relay_to = 250_000, 9
    assert relay_hint(s, False, False, now) is None                # 已经接力过
