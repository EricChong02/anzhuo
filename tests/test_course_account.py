"""合作账号必须只能预览一课；反馈沿用受限执行入口。"""
import asyncio
import json
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_ext_course import _bridge, _course, _login
from remote_claude.sessions import Session


def test_course_account_boundaries(tmp_path):
    async def go():
        seen = []
        async def upstream(r):
            seen.append(r.path)
            return web.Response(text="<body>本课</body>", content_type="text/html")
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", upstream)
        up = TestServer(app)
        await up.start_server()
        b, root = _bridge(tmp_path, str(up.make_url("")).rstrip("/"))
        lesson = _course(root, "_drafts/a", "合作课程")
        _course(root, "_drafts/b", "另一课")
        (lesson / "leak").symlink_to(root / "_drafts/b", target_is_directory=True)
        b.web.accounts["user"].update(root=str(lesson), course="_drafts/a")
        admin = b.registry.add(Session(num=b.registry.reserve_num(), name="合作课程", cwd=str(root)))
        own = b.registry.add(Session(num=b.registry.reserve_num(), name="合作课程", cwd=str(lesson), owner="user", root=str(lesson)))
        c = await _login(b, "user", "demo-guest-test")
        url = "/workspace/_drafts/a/course.json"
        try:
            r = await c.get("/studio-preview.html", params={"course": url})
            assert r.status == 200 and "feedback.js" in await r.text()
            for path in [url, "/assets/css/tokens.css", "/engine/course-engine.js", "/shared/student-drafts.js", "/shared/canvas-chrome.js", "/studio-rehearse.js", "/teacher/lecture-cues.js", "/app/lecture-reveal.js", "/courses/_shared/x.js", "/_x/ext/course/static/feedback.js"]:
                assert (await c.get(path)).status == 200, path
            for path in ["/workspace/_drafts/b/course.json", "/workspace/_drafts/a/leak/course.json", "/api/drafts", "/api/students", "/ws/teacher", "/teacher.html", "/api/lesson-preview/check", "/_x/admin", "/_x/ext/course/static/course.js"]:
                assert (await c.get(path)).status in (403, 404), path
            assert (await c.get("/studio-preview.html", params={"course": "/workspace/_drafts/b/course.json"})).status == 403
            assert (await c.get("/_x/api/thread/" + str(admin.num))).status == 404
            d = await (await c.get("/_x/ext/course/api/course", params={"course": url})).json()
            assert d["num"] == own.num
            assert (await c.get("/_x/ext/course/api/course", params={"course": "/workspace/_drafts/b/course.json"})).status == 403
            called = []
            async def send_fenced(request, acct, data, text, streamed):
                called.append((request["owner"], acct, data))
                return web.json_response({"ok": True, "num": own.num})
            b.web._send_fenced = send_fenced
            payload = {"course": url, "section": "sec-1", "screen": 2, "text": "这里字大一点"}
            r = await c.post("/_x/ext/course/api/feedback", json=payload)
            assert r.status == 200 and (await r.json())["num"] == own.num
            assert called[0][:2] == ("user", "user") and called[0][2]["num"] == own.num
            assert "这里字大一点" in called[0][2]["text"]
            assert b.exts[0].api.data(own)["feedback"]["screen"] == 2
            assert (await c.post("/_x/ext/course/api/feedback", json={**payload, "course": "/workspace/_drafts/b/course.json"})).status == 403
            b.web.base = "https://x.example"
            assert (await c.post("/_x/ext/course/api/feedback", json=payload, headers={"Origin": "https://evil.invalid"})).status == 403
            b.web.accounts["user"].pop("course")
            assert (await c.get(url)).status == 403
        finally:
            await c.close()
            await up.close()
    asyncio.run(go())


def test_course_account_no_personal_quota(tmp_path):
    b, _ = _bridge(tmp_path)
    assert b.web.quota("user") is not None
    b.web.accounts["user"]["quota_exempt"] = True
    assert b.web.quota("user") is None
    assert b.web.over_quota("user") == ""
    assert b.web.role("user") == "user"
    b.web.accounts["user"]["quota_exempt"] = False
    assert b.web.quota("user") is not None


def test_account_default_model_and_manual_override(tmp_path):
    async def go():
        b, root = _bridge(tmp_path)
        b.web.accounts["user"].update(default_model="opus", default_effort="medium", allowed_models=["opus", "sonnet"])
        calls = []
        async def start(*args, **kwargs):
            calls.append(kwargs)
        b.start_session = start
        c = await _login(b, "user", "demo-guest-test")
        try:
            r = await c.post("/_x/api/send", json={"text": "默认模型"})
            assert r.status == 200
            from remote_claude.engines import MODELS
            assert calls[-1]["model"] == MODELS["opus"]
            assert calls[-1]["effort"] == "medium"
            assert calls[-1]["owner"] == "user" and calls[-1]["root"]
            r = await c.post("/_x/api/send", json={"text": "手动选择", "model": "sonnet", "effort": "high"})
            assert r.status == 200
            assert calls[-1]["model"] == MODELS["sonnet"] and calls[-1]["effort"] == "high"
            listing = await (await c.get("/_x/api/sessions")).json()
            assert [m[0] for m in listing["models"]] == ["opus", "sonnet"]
            assert listing["me"]["default_model"] == "opus"
            assert (await c.post("/_x/api/send", json={"text": "不允许的模型", "model": "haiku"})).status == 403
            session = b.registry.add(Session(num=b.registry.reserve_num(), name="test", cwd=str(root), owner="user", root=str(root)))
            assert (await c.post(f"/_x/api/settings/{session.num}", json={"model": "gpt"})).status == 403
            assert (await c.post(f"/_x/api/settings/{session.num}", json={"model": "sonnet", "effort": "medium"})).status == 200
            assert session.model == MODELS["sonnet"]

        finally:
            await c.close()
    asyncio.run(go())
