"""不连钉钉、不调 Claude，走一遍：开对话、选文件夹、引用回复、列表、报告推送。"""
import asyncio
import sqlite3

from remote_claude.app import Bridge
from remote_claude.config import Config
from remote_claude.sessions import Runner


class FakeDT:
    secret = "x"

    def __init__(self):
        self.sent = []

    async def send_markdown(self, uid, title, text):
        self.sent.append((title, text))

    async def send_image(self, uid, path):
        self.sent.append(("图片", str(path)))

    async def download(self, code, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG")
        return dest


def msg(text, quoted=None):
    d = {"msgtype": "text", "text": {"content": text}, "senderStaffId": "me", "senderNick": "庄毅", "conversationType": "1"}
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


def test_auto_route(tmp_path, monkeypatch):
    from remote_claude import router
    (tmp_path / "课程").mkdir()
    cfg = Config(client_id="x", allowed_user_ids=["me"], folders={"课程根": str(tmp_path / "课程")},
                 search_roots=[str(tmp_path)], scratch_root=str(tmp_path / "scratch"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, s.cwd, job.text))
        turn.final_text = "好的"

    async def no_usage(self, force=False):
        pass

    answers = iter([
        router.Route("new", None, "课程根"),     # 新事 → 自动在课程根新开 #1
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
        await b.on_raw(msg("把第二课封面标题改大"))
        await asyncio.sleep(0.05)
        await b.on_raw(msg("再大一点"))  # 一看就是回复：不请小E判断，直接接当前对话
        await asyncio.sleep(0.05)
        await b.on_raw(msg("顺便查下天气"))
        assert "回编号" in dt.sent[-1][1]
        await b.on_raw(msg("2"))  # 选新开
        await asyncio.sleep(0.05)
        await b.on_raw(msg("去那个文件夹看看"))
        assert "回编号" in dt.sent[-1][1]
        await b.on_raw(msg("1"))  # 选接着当前
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert seen[0][:2] == (1, str(tmp_path / "课程"))
    assert seen[1][0] == 1 and seen[1][2] == "再大一点"
    assert seen[2][0] == 2 and "scratch" in seen[2][1]
    assert seen[3][0] == 2 and seen[3][2] == "去那个文件夹看看"
