"""小E 网页：一次性链接登录、过程页、试课预览的放行名单与转发。"""
import asyncio
import time

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
            assert "开发" in await (await c.get("/_x/")).text()
            w.preview_root = str(tmp_path / "root")
            (tmp_path / "root" / "_drafts" / "l9").mkdir(parents=True)
            (tmp_path / "root" / "_drafts" / "l9" / "course.json").write_text('{"title": "淘宝 2003"}')
            idx = await (await c.get("/_x/")).text()
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
