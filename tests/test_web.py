"""小E 网页：一次性链接登录、过程页、试课预览的放行名单与转发。"""
import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_claude.report import Turn
from remote_claude.web import Web, course_previews, preview_allowed


def test_preview_allowed():
    ok = [("GET", "/studio-preview.html"), ("GET", "/engine/x.js"), ("GET", "/workspace/_drafts/a/course.json"),
          ("POST", "/api/lesson-preview/check"), ("GET", "/api/drafts"), ("GET", "/assets/a.css")]
    bad = [("GET", "/teacher.html"), ("GET", "/"), ("GET", "/api/teacher/classes"), ("GET", "/api/students"),
           ("GET", "/api/draftsx"), ("GET", "/ws/teacher"), ("POST", "/engine/x.js"), ("GET", "/../etc/passwd"),
           ("GET", "/library.html"), ("POST", "/api/login")]
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
        w = Web("http://x.example", 0, tmp_path, f"http://127.0.0.1:{up.port}", "")
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        t = Turn(started=time.time() - 120, ended=time.time(), request="改<封面>", final_text="**做了什么**：改了标题",
                 images=[str(img)], timeline=[{"t": time.time(), "kind": "do", "text": "跑命令：测试", "detail": "pytest -q"}])
        tid = w.save_turn("#4 开发", t, str(tmp_path), "做完了")
        link = w.link(f"/_x/t/{tid}")
        token = link.rsplit("/", 1)[1]
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            assert (await c.get(f"/_x/t/{tid}")).status == 403            # 没登录
            r = await c.get(f"/_x/l/{token}", allow_redirects=False)      # 一次性链接换登录
            assert r.status == 302 and r.headers["Location"] == f"/_x/t/{tid}"
            page = await (await c.get(f"/_x/t/{tid}")).text()
            assert "改&lt;封面&gt;" in page and "<b>做了什么</b>" in page and "pytest -q" in page
            assert (await c.get(f"/_x/t/{tid}/img/0")).status == 200
            assert (await c.get(f"/_x/t/{tid}/img/5")).status == 404
            assert "开发" in await (await c.get("/_x/")).text()
            r = await c.get("/studio-preview.html?course=/workspace/a/course.json")
            assert await r.text() == "PREVIEW" and seen["cookie"] is None and seen["path"].startswith("/studio-preview")
            await c.get("/workspace/a/course.json", headers={"X-Forwarded-For": "1.2.3.4"})
            await c.get("/api/teacher/answers", headers={"X-Forwarded-For": "1.2.3.4"})
            assert seen["xff"]["/workspace/a/course.json"] is None          # 课件当本机直连
            assert seen["xff"]["/api/teacher/answers"] == "1.2.3.4"         # 教师接口照旧当外来
            assert (await c.get("/teacher.html")).status == 403
            assert (await c.get("/api/teacher/classes")).status == 403
            c.session.cookie_jar.clear()
            assert (await c.get(f"/_x/l/{token}", allow_redirects=False)).status == 403  # 用过的链接，换个浏览器不行
            assert (await c.get("/studio-preview.html")).status == 403
        finally:
            await c.close()
            await up.close()
            if w._client:
                await w._client.close()

    asyncio.run(go())
