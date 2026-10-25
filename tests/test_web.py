"""小E 网页：一次性链接登录、过程页、试课预览的放行名单与转发。"""
import asyncio
import time
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_claude.report import Turn
from remote_claude.web import Web, check_password, course_previews, hash_password, preview_allowed


def test_preview_allowed():
    ok = [("GET", "/studio-preview.html"), ("GET", "/engine/x.js"), ("GET", "/workspace/_drafts/a/course.json"),
          ("POST", "/api/lesson-preview/check"), ("GET", "/api/drafts"), ("GET", "/assets/a.css"),
          ("GET", "/workspace/_drafts/a/fragments/section-2.html"), ("GET", "/courses/topics/bx1/a/index.html")]
    bad = [("GET", "/teacher.html"), ("GET", "/"), ("GET", "/api/teacher/classes"), ("GET", "/api/students"),
           ("GET", "/api/draftsx"), ("GET", "/ws/teacher"), ("POST", "/engine/x.js"), ("GET", "/../etc/passwd"),
           ("GET", "/library.html"), ("POST", "/api/login"), ("GET", "/student.html"),
           ("POST", "/workspace/_drafts/a/fragments/section-2.html"), ("GET", "/workspace/../teacher.html")]
    assert all(preview_allowed(m, p) for m, p in ok)
    assert not any(preview_allowed(m, p) for m, p in bad)


def test_course_previews(tmp_path):
    lesson = tmp_path / "_drafts" / "l01"
    (lesson / "fragments").mkdir(parents=True)
    (lesson / "course.json").write_text("{}")
    files = [str(lesson / "fragments" / "s1.html"), str(lesson / "course.json"), "/elsewhere/a.py"]
    got = course_previews(files, str(tmp_path))
    assert got == [("l01", "/studio-preview.html?course=/workspace/_drafts/l01/course.json")]
    assert course_previews(files, "") == []


def test_password_hash():
    h = hash_password("020928", iters=1000)
    assert h.startswith("pbkdf2$1000$") and "020928" not in h
    assert check_password("020928", h) and not check_password("020929", h) and not check_password("x", "坏的")


def test_login_pages_and_proxy(tmp_path):
    seen = {}

    async def upstream(request):
        seen.setdefault("xff", {})[request.path] = request.headers.get("X-Forwarded-For")
        seen["cookie"] = request.headers.get("Cookie")
        seen["path"] = request.path_qs
        return web.Response(text="PREVIEW", content_type="text/html")

    async def go():
        up_app = web.Application()
        up_app.router.add_route("*", "/{tail:.*}", upstream)
        up = TestServer(up_app)
        await up.start_server()
        w = Web("http://x.example", 0, tmp_path, f"http://127.0.0.1:{up.port}", "",
                user="18000000000", pw_hash=hash_password("pw-ok", iters=1000))
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        t = Turn(started=time.time() - 120, ended=time.time(), request="改<封面>", final_text="**做了什么**：改了标题",
                 images=[str(img)], timeline=[{"t": time.time(), "kind": "do", "text": "跑命令：测试", "detail": "pytest -q"}])
        tid = w.save_turn("#4 开发", t, str(tmp_path), "做完了")
        assert w.link(f"/_x/t/{tid}") == f"http://x.example/_x/t/{tid}"   # 固定网址
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            r = await c.get(f"/_x/t/{tid}", allow_redirects=False)            # 没登录 → 登录页，带着回来的地址
            assert r.status == 302 and r.headers["Location"].startswith("/_x/login?next=")
            assert (await c.get("/api/drafts")).status == 401
            login = await (await c.get(r.headers["Location"])).text()
            assert 'autocomplete="current-password"' in login
            bad = await c.post("/_x/login", data={"username": "18000000000", "password": "错", "next": "/x"},
                               allow_redirects=False)
            assert bad.status == 401
            r = await c.post("/_x/login", data={"username": "18000000000", "password": "pw-ok", "next": f"/_x/t/{tid}"},
                             allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == f"/_x/t/{tid}"
            page = await (await c.get(f"/_x/t/{tid}")).text()
            assert "改&lt;封面&gt;" in page and "<b>做了什么</b>" in page and "pytest -q" in page
            assert (await c.get(f"/_x/t/{tid}/img/0")).status == 200
            assert (await c.get(f"/_x/t/{tid}/img/5")).status == 404
            assert (await c.get("/", allow_redirects=False)).headers["Location"] == "/_x/"
            assert "开发" in await (await c.get("/_x/history")).text()
            w.preview_root = str(tmp_path / "root")
            (tmp_path / "root" / "_drafts" / "l9").mkdir(parents=True)
            (tmp_path / "root" / "_drafts" / "l9" / "course.json").write_text('{"title": "淘宝 2003"}')
            idx = await (await c.get("/_x/history")).text()
            assert "试课：淘宝 2003" in idx and "/workspace/_drafts/l9/course.json" in idx
            r = await c.get("/studio-preview.html?course=/workspace/a/course.json")
            assert await r.text() == "PREVIEW" and seen["cookie"] is None and seen["path"].startswith("/studio-preview")
            await c.get("/workspace/a/course.json", headers={"X-Forwarded-For": "1.2.3.4"})
            await c.get("/api/teacher/answers", headers={"X-Forwarded-For": "1.2.3.4"})
            assert seen["xff"]["/workspace/a/course.json"] is None          # 课件当本机直连
            assert seen["xff"]["/api/teacher/answers"] == "1.2.3.4"         # 教师接口照旧当外来
            assert (await c.get("/teacher.html")).status == 403
            assert (await c.get("/api/teacher/classes")).status == 403
            # 登录后跳转只认本站路径，不能借登录页跳到外站
            r = await c.get("/_x/login?next=//evil.example/x", allow_redirects=False)
            assert r.headers["Location"] == "/_x/"
            # 换个浏览器（没 Cookie）：连错 5 次锁住，锁住时对的密码也不行
            c.session.cookie_jar.clear()
            for _ in range(5):
                await c.post("/_x/login", data={"username": "18000000000", "password": "错"},
                             headers={"X-Forwarded-For": "6.6.6.6"})
            r = await c.post("/_x/login", data={"username": "18000000000", "password": "pw-ok"},
                             headers={"X-Forwarded-For": "6.6.6.6"}, allow_redirects=False)
            assert r.status == 429
            r = await c.post("/_x/login", data={"username": "18000000000", "password": "pw-ok"},
                             headers={"X-Forwarded-For": "6.6.6.6, 7.7.7.7"}, allow_redirects=False)
            assert r.status == 302                                           # 只认最后一个来源，别的地址不受牵连
        finally:
            await c.close()
            await up.close()
            if w._client:
                await w._client.close()

    asyncio.run(go())


def test_thread_history():
    from types import SimpleNamespace as M
    from remote_claude import thread
    msgs = [
        M(type="user", message={"content": "This session is being continued from a previous conversation"}),
        M(type="user", message={"content": "<command-name>/compact</command-name>"}),
        M(type="user", message={"content": [{"type": "text", "text": "把封面标题改大"}, {"type": "image", "source": {}}]}),
        M(type="assistant", message={"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "我先看看"}, {"type": "tool_use", "name": "Read"}]}),
        M(type="user", message={"content": [{"type": "tool_result", "content": "..."}]}),
        M(type="assistant", message={"content": [{"type": "tool_use", "name": "Edit"}]}),
        M(type="assistant", message={"content": [{"type": "text", "text": "改好了"}]}),
        M(type="user", message={"content": "好的"}),
        M(type="user", message={"content": "[SYSTEM NOTIFICATION - NOT USER INPUT] 后台任务结束"}),
        M(type="assistant", message={"content": [{"type": "text", "text": "No response requested."}]}),
    ]
    items = thread.history(msgs)
    assert items == [{"who": "note", "text": "（更早的对话内容已压缩）"},
                     {"who": "you", "text": "[图片]\n把封面标题改大"},
                     {"who": "claude", "text": "改好了", "steps": 2,
                      "process": [{"k": "say", "text": "我先看看"}, {"k": "do", "text": "看 文件", "detail": ""},
                                  {"k": "do", "text": "改 文件", "detail": ""}]},
                     {"who": "you", "text": "好的"}]
    thread.attach_records(items, [{"id": "6-1", "title": "做完了", "previews": [["l01", "/p"]]}])
    assert items[2]["tid"] == "6-1" and items[2]["previews"] == [["l01", "/p"]]


def test_chat_api(tmp_path, monkeypatch):
    """网页对话框：列表、读对话、发话（带图）、新开、停下；没登录给 401；外站来源的 POST 拒绝。"""
    from types import SimpleNamespace as NS
    from remote_claude.sessions import Session

    class FakeRunner:
        def __init__(self):
            self.turns, self.stopped = {}, []
            self.limits, self.default_effort = {"five_hour": (0.37, "10月25日 21:00"), "seven_day": (0.22, None)}, "medium"

        def busy(self, n):
            return n in self.turns

        async def stop(self, n):
            self.stopped.append(n)
            return True

    class FakeBridge:
        def __init__(self):
            s6 = Session(num=6, name="做课", cwd=str(tmp_path), awaiting="要我按 C 来做吗？", claude_id="sid6")
            s7 = Session(num=7, name="清理", cwd=str(tmp_path))
            self.registry = NS(sessions={6: s6, 7: s7}, visible=lambda: [s6, s7], get=lambda n: {6: s6, 7: s7}.get(n),
                               current=6, next_num=8, save=lambda: None)
            self.runner = FakeRunner()
            self.runner.turns[7] = Turn(started=time.time() - 130, request="清理下载", steps=["看 a", "跑命令：du"])
            self.cfg = NS(folders={"课程根": "/x"})
            self.user_id, self.sent, self.new, self.outgoing, self.said = "me", [], [], {}, []

        async def say(self, title, text):
            self.said.append((title, text))

        def record(self, *a, **k):
            pass

        async def _message(self, cmd, inc, routed=False):
            self.sent.append((cmd.num, cmd.text, list(inc.local_images)))
            self.outgoing.setdefault(cmd.num, []).append({"t": 0, "text": cmd.text})

        async def _new(self, tokens, text, inc, note="", kind=None, model=None, effort=None):
            self.new.append((tokens, text))
            self.new_model = (model, effort)
            self.registry.sessions[8] = Session(num=8, name="新", cwd="/x")

    async def go():
        root = tmp_path / "root"
        (root / "_drafts" / "c1").mkdir(parents=True)
        (root / "_drafts" / "c1" / "course.json").write_text('{"title": "淘宝 2003"}')
        w = Web("http://x.example", 0, tmp_path, "", str(root), user="u", pw_hash=hash_password("p", iters=1000))
        w.bridge = b = FakeBridge()
        b.registry.get = lambda n: b.registry.sessions.get(n)
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            assert (await c.get("/_x/api/sessions")).status == 401
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            d = await (await c.get("/_x/api/sessions")).json()
            assert [r["num"] for r in d["sessions"]] == [6, 7]                 # 等你回答的排最前
            assert d["sessions"][0]["unread"] is True                          # 做完了还没看
            await c.get("/_x/api/thread/6")
            d = await (await c.get("/_x/api/sessions")).json()
            assert d["sessions"][0]["unread"] is False                         # 打开看过就不算了
            assert d["sessions"][1]["detail"].startswith("在做 · 2 分钟 · 第 2 步")
            assert d["limits"] == [{"name": "5 小时", "pct": 37, "reset": "10月25日 21:00"}, {"name": "本周", "pct": 22, "reset": ""}]
            assert "小E" in await (await c.get("/_x/")).text()
            t7 = await (await c.get("/_x/api/thread/7")).json()
            assert t7["live"]["steps"] == ["看 a", "跑命令：du"] and t7["decision"] is None
            assert t7["usage"] == {"model": "默认模型", "effort": "中", "context": None, "mkey": "", "ekey": "medium"}
            png = "data:image/png;base64," + __import__("base64").b64encode(b"\x89PNG").decode()
            r = await (await c.post("/_x/api/send", json={"num": 7, "text": "顺便看看桌面", "images": [png]})).json()
            assert r == {"ok": True, "injected": True}
            assert b.sent[0][:2] == (7, "顺便看看桌面") and b.sent[0][2][0].endswith(".png")
            assert b.said == []                                                    # 钉钉不转述你在网页说的话
            t7 = await (await c.get("/_x/api/thread/7")).json()
            assert t7["items"][-1] == {"who": "you", "text": "顺便看看桌面", "pending": True}   # 记录里还没有，也先显示
            r = await (await c.post("/_x/api/send", json={"text": "查一下天气", "folder": ""})).json()
            assert r["num"] == 8 and b.new == [([], "查一下天气")]
            # 新对话“在哪做”：接着做某门课 / 小E 自动判断 / 不碰文件
            d = await (await c.get("/_x/api/sessions")).json()
            assert d["drafts"][0]["dir"] == "c1" and d["drafts"][0]["title"] == "淘宝 2003"
            await c.post("/_x/api/send", json={"text": "封面改大", "course": "c1"})
            assert b.new[-1] == ([str(root)], "（这件事是关于课程《淘宝 2003》，目录 _drafts/c1/）\n封面改大")
            from remote_claude import folders, router
            async def fake_route(text, *a, **k):
                return router.Route("new", None, "课程根", "", "dev", "")
            monkeypatch.setattr(router, "route", fake_route)
            monkeypatch.setattr(folders, "candidates", lambda tok, cfg: [Path("/x")] if tok == "课程根" else [])
            await c.post("/_x/api/send", json={"text": "新做一节课", "folder": "auto"})
            assert b.new[-1] == (["课程根"], "新做一节课")
            await c.post("/_x/api/send", json={"text": "查天气", "folder": "scratch"})
            assert b.new[-1] == ([], "查天气")
            # 选模型和思考深度：新开时带上；已有对话改了存下来，下一轮生效；不认得的不理
            await c.post("/_x/api/send", json={"text": "查天气", "folder": "scratch", "model": "sonnet", "effort": "high"})
            assert b.new_model == ("claude-sonnet-5-5", "high")
            await c.post("/_x/api/send", json={"text": "查天气", "folder": "scratch", "model": "gpt", "effort": "超高"})
            assert b.new_model == (None, None)
            b.registry.save = lambda: None
            assert (await (await c.post("/_x/api/settings/7", json={"model": "haiku", "effort": "xhigh"})).json())["ok"]
            assert (b.registry.sessions[7].model, b.registry.sessions[7].effort) == ("claude-haiku-5-5", "xhigh")
            u = (await (await c.get("/_x/api/thread/7")).json())["usage"]
            assert (u["mkey"], u["ekey"]) == ("haiku", "xhigh")
            # 手机通知：后台脚本不登录也能取；订阅要像样（https 地址 + keys）才收
            assert "showNotification" in await (await c.get("/_x/sw.js")).text()
            sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "x", "auth": "y"}}
            assert (await c.post("/_x/api/push", json=sub)).status == 200
            assert (await c.post("/_x/api/push", json=sub)).status == 200
            assert (await c.post("/_x/api/push", json={"endpoint": "http://evil"})).status == 400
            assert w.push.subs == [sub]
            # 退出登录：这台设备的登录和通知都清掉，再访问要重新登录
            r = await c.post("/_x/api/logout", json={"endpoint": sub["endpoint"]})
            assert r.status == 200 and w.push.subs == [] and w.store["sessions"] == {}
            assert (await c.get("/_x/api/sessions")).status == 401
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            assert len(w.store["sessions"]) == 2
            await c.post("/_x/api/logout", json={"all": True})
            assert w.store["sessions"] == {}
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            assert (await (await c.post("/_x/api/stop/7", json={})).json())["stopped"] and b.runner.stopped == [7]
            assert (await c.post("/_x/api/send", json={"text": ""})).status == 400
        finally:
            await c.close()

    asyncio.run(go())


def test_files_delivered_and_uploaded(tmp_path):
    """交给你的文件：复制到小E 自己的文件夹、过程页和对话里能下载（原文件名）；网页传上来的文件存进收件箱。"""
    import base64 as b64
    from types import SimpleNamespace as NS

    async def go():
        w = Web("http://x.example", 0, tmp_path, "", "", user="u", pw_hash=hash_password("p", iters=1000))
        src = tmp_path / "tmpdir" / "研究生毕业证.pdf"
        src.parent.mkdir()
        src.write_bytes(b"%PDF-1.4 fake")
        t = Turn(started=time.time() - 60, ended=time.time(), request="发毕业证", final_text="发了", files=[str(src)])
        tid = w.save_turn("#7 毕业证", t, str(tmp_path), "做完了")
        src.unlink()                                                      # 原处被清掉也不影响下载
        rec = w._records(7)[0]
        assert rec["files"][0]["name"] == "研究生毕业证.pdf" and rec["files"][0]["size"] == 13
        sent = []

        async def _message(cmd, inc, routed=False):
            sent.append(cmd.text)
        async def say(*a):
            pass
        w.bridge = NS(runner=NS(turns={}), user_id="me", record=lambda *a, **k: None, _message=_message,
                      outgoing={}, say=say, registry=NS(get=lambda n: None))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            r = await c.get(f"/_x/t/{tid}/file/0")
            assert r.status == 200 and await r.read() == b"%PDF-1.4 fake"
            assert "attachment" in r.headers["Content-Disposition"] and "%E7%A0%94" in r.headers["Content-Disposition"]
            assert "研究生毕业证.pdf" in await (await c.get(f"/_x/t/{tid}")).text()
            data = "data:application/pdf;base64," + b64.b64encode(b"hello").decode()
            r = await (await c.post("/_x/api/send", json={"num": 7, "text": "看看这个", "files": [{"name": "../合同.pdf", "data": data}]})).json()
            assert r["ok"] and "附件已存到" in sent[0] and sent[0].endswith("合同.pdf）")
            saved = sent[0].split("附件已存到：")[1].rstrip("）")
            assert Path(saved).read_bytes() == b"hello" and Path(saved).parent == tmp_path / "inbox" / "web"
        finally:
            await c.close()

    asyncio.run(go())
