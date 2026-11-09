"""做课扩展（extensions/course，导出开源版时连同本文件一起不带）：试课预览的放行与转发、试课链接、课程库、试课反馈。"""
import asyncio
import json
import os
import time
from types import SimpleNamespace as NS

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_claude.app import Bridge
from remote_claude.config import Config
from remote_claude.extensions.course import course_previews, preview_allowed
from remote_claude.report import Turn
from remote_claude.sessions import Session
from remote_claude.web import hash_password


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


def _course(root, rel, title="", vis="hidden", t=None):
    d = root / rel
    (d / "fragments").mkdir(parents=True, exist_ok=True)
    (d / "course.json").write_text(json.dumps({"title": title or rel.split("/")[-1].upper(), "catalog": {"visibility": vis}}))
    if t is not None:
        os.utime(d / "course.json", (t, t))
    return d


def _bridge(tmp_path, upstream="http://127.0.0.1:9", **ext):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    conf = {"course": {"root": str(root), "upstream": upstream, **ext}}
    b = Bridge(Config(client_id="t", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch"), ext=conf),
               NS(secret="x"), tmp_path / "data")
    b.web.base = "http://x.example"
    b.web.accounts = {"admin": {"role": "admin", "root": "", "hash": hash_password("demo-admin-test", iters=1000)},
                      "user": {"role": "user", "root": str(tmp_path / "rw"), "share": 0.2, "hash": hash_password("demo-guest-test", iters=1000)}}
    b.sent, b.new = [], []

    async def message(cmd, inc, routed=False):
        b.sent.append((cmd.num, cmd.text, list(inc.local_images), inc.via))

    async def new(tokens, text, inc, note="", kind=None, model=None, effort=None, **k):
        b.new.append((tokens, text, kind))
        s = b.registry.add(Session(num=b.registry.reserve_num(), name="新", cwd=tokens[0] if tokens else ""))
        return s

    b.message, b.new_session = message, new
    return b, root


async def _login(b, name="admin", pw="demo-admin-test"):
    c = TestClient(TestServer(b.web.app()))
    await c.start_server()
    r = await c.post("/_x/login", data={"username": name, "password": pw}, allow_redirects=False)
    assert r.status == 302, await r.text()
    return c


def test_loaded_only_when_configured(tmp_path):
    b, _ = _bridge(tmp_path)
    assert [e.name for e in b.exts] == ["course"]
    off = Bridge(Config(client_id="t", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s2"),
                        ext={"course": {"root": str(tmp_path), "enabled": False}}), NS(secret="x"), tmp_path / "d2")
    none = Bridge(Config(client_id="t", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s3")), NS(secret="x"), tmp_path / "d3")
    assert off.exts == [] and none.exts == []


def test_course_list_stages(tmp_path):
    """侧栏“课”：草稿和已上架的都列，标出走到哪了；发布后又改过的标出来。"""
    b, root = _bridge(tmp_path)
    now = time.time()
    _course(root, "_drafts/a", t=now - 10)
    _course(root, "topics/bx1/b", vis="published", t=now - 100)
    _course(root, "topics/bx1/c", vis="published", t=now - 5000)
    _course(root, "topics/bx2/d", t=now - 9000)
    (root / "_published.json").write_text(json.dumps({"courses": {"b": {"since": {"version": now - 4000}},
                                                                  "c": {"since": {"version": now - 4000}}}}))
    c = b.exts[0]
    got = {d["dir"]: (d["stage"], d["dirty"], d["rel"]) for d in c.drafts()}
    assert [d["dir"] for d in c.drafts()] == ["a", "b", "c", "d"]
    assert got == {"a": ("draft", False, "_drafts/a"), "b": ("published", True, "topics/bx1/b"),
                   "c": ("published", False, "topics/bx1/c"), "d": ("hidden", False, "topics/bx2/d")}
    assert "/workspace/topics/bx1/b/course.json" in c.drafts()[1]["url"].replace("%2F", "/")


def test_moved_course_link(tmp_path):
    """早先记下的草稿试课链接：课上架挪进 topics 后自动跳到新位置；还在草稿里的不动。"""
    b, root = _bridge(tmp_path)
    _course(root, "topics/data/c1")
    _course(root, "_drafts/c2")
    c = b.exts[0]
    assert c._moved_course("/workspace/_drafts/c1/course.json") == "/workspace/topics/data/c1/course.json"
    assert c._moved_course("/workspace/_drafts/c2/course.json") == ""
    assert c._moved_course("/workspace/_drafts/none/course.json") == ""


def test_proxy_links_and_pages(tmp_path):
    """改到课件 → 记录里有试课链接（报告、过程页、历史页、对话框都有）；试课页经远窗转发，插反馈脚本。"""
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
        b, root = _bridge(tmp_path, f"http://127.0.0.1:{up.port}")
        lesson = _course(root, "_drafts/l9", "淘宝 2003")
        s = b.registry.add(Session(num=b.registry.reserve_num(), name="做课", cwd=str(root)))
        t = Turn(started=time.time() - 60, ended=time.time(), request="封面改大", final_text="改好了",
                 changed_files=[str(lesson / "fragments" / "s1.html")])
        tid = b.web.save_turn(s.label, t, s.cwd, "做完了")
        links = b.web.links_of(b.web._turn(tid))
        assert links == [{"label": "试课", "url": "/studio-preview.html?course=/workspace/_drafts/l9/course.json",
                          "title": "淘宝 2003", "icon": "play", "key": "l9", "ext": "course"}]
        assert b.exts[0].course_session("l9", "淘宝 2003") is s          # 发过这门课试课链接的对话
        c = await _login(b)
        try:
            assert "试课：淘宝 2003" in await (await c.get(f"/_x/t/{tid}")).text()
            idx = await (await c.get("/_x/history")).text()
            assert "最近的课" in idx and "/workspace/_drafts/l9/course.json" in idx
            d = await (await c.get("/_x/api/sessions")).json()
            assert d["ext"]["course"]["drafts"][0]["title"] == "淘宝 2003" and d["ext"]["course"]["courses"][0]["num"] == s.num
            page = await (await c.get("/_x/")).text()
            assert '<script src="/_x/ext/course/static/course.js"></script></body>' in page
            assert "反馈这一屏" in await (await c.get("/_x/ext/course/static/feedback.js")).text()
            assert (await c.get("/_x/ext/course/static/vendor/modern-screenshot.js")).status == 200
            assert (await c.get("/_x/ext/course/static/nope.js")).status == 404
            r = await c.get("/studio-preview.html?course=/workspace/a/course.json")
            assert await r.text() == 'PREVIEW<script src="/_x/ext/course/static/feedback.js"></script>'
            assert seen["cookie"] is None and seen["path"].startswith("/studio-preview")
            await c.get("/workspace/a/course.json", headers={"X-Forwarded-For": "1.2.3.4"})
            await c.get("/api/teacher/answers", headers={"X-Forwarded-For": "1.2.3.4"})
            assert seen["xff"]["/workspace/a/course.json"] is None          # 课件当本机直连
            assert seen["xff"]["/api/teacher/answers"] == "1.2.3.4"         # 教师接口照旧当外来
            assert (await c.get("/teacher.html")).status == 403
            assert (await c.get("/api/teacher/classes")).status == 403
            await up.close()
            assert (await c.get("/studio-preview.html")).status == 502      # 本机预览没开
        finally:
            await c.close()

    asyncio.run(go())


def test_report_link_and_old_records(tmp_path):
    """钉钉、通知里的链接写“试课：课名”；2026-11-08 以前的记录（previews）照样认。"""
    b, root = _bridge(tmp_path)
    _course(root, "_drafts/l1", "数据从哪来")
    old = {"id": "3-1", "previews": [["l1", "/studio-preview.html?course=x"]]}
    assert b.web.links_of(old) == [{"label": "试课", "url": "/studio-preview.html?course=x", "title": "数据从哪来",
                                    "icon": "play", "key": "l1", "ext": "course"}]
    s = b.registry.add(Session(num=b.registry.reserve_num(), name="做课", cwd=str(root)))
    t = Turn(started=time.time() - 5, ended=time.time(), request="改", final_text="好了",
             changed_files=[str(root / "_drafts" / "l1" / "course.json")])
    b.web.enabled_ok = True
    said = []

    async def say(title, text, board=True):
        said.append(text)
    b.say = say
    b.web.watching = lambda n: False

    async def no_push(*a, **k):
        return 0
    b.web.notify = no_push
    asyncio.run(b._turn_done(s, t))
    assert t.links[1][0] == "试课：数据从哪来"


def test_feedback_follows_recent_manual_work(tmp_path):
    """同名对话里，反馈跟着你最近亲手在用的那个走；被试课反馈喂得再新的那个不算“最近在用”。"""
    b, root = _bridge(tmp_path)
    lesson = _course(root, "_drafts/c1", "淘宝 2003")
    now = time.time()
    old = b.registry.add(Session(num=b.registry.reserve_num(), name="做课：淘宝 2003", cwd=str(root), updated=now))
    new = b.registry.add(Session(num=b.registry.reserve_num(), name="做课：淘宝 2003", cwd=str(root), updated=now - 60))
    link = Turn(started=now - 3600, ended=now - 3500, request="封面改大", final_text="好了",
                changed_files=[str(lesson / "fragments" / "s1.html")])
    b.web.save_turn(old.label, link, old.cwd, "做完了")          # 老对话发过这门课的试课链接
    b.web.save_turn(old.label, Turn(started=now - 20, ended=now - 10, request="（试课反馈）标题太小",
                                    final_text="改好了"), old.cwd, "做完了")   # 最近只收到反馈
    b.web.save_turn(new.label, Turn(started=now - 400, ended=now - 300, request="参考图留 9 幅",
                                    final_text="好了"), new.cwd, "做完了")     # 你 5 分钟前亲手改过
    assert b.exts[0].course_session("c1", "淘宝 2003") is new


def test_feedback(tmp_path):
    """试课页反馈：带上课、节、屏，交给正在做这门课的对话；没有就在课程根新开；记下反馈的那一屏。"""
    async def go():
        b, root = _bridge(tmp_path)
        _course(root, "_drafts/c1", "淘宝 2003")
        s6 = b.registry.add(Session(num=b.registry.reserve_num(), name="做课：淘宝 2003 数据工程师", cwd=str(root)))
        b.registry.add(Session(num=b.registry.reserve_num(), name="清理下载", cwd=str(tmp_path)))
        c = await _login(b)
        try:
            png = "data:image/png;base64," + __import__("base64").b64encode(b"\x89PNG").decode()
            post = lambda body: c.post("/_x/ext/course/api/feedback", json=body)
            r = await (await post({"course": "/workspace/_drafts/c1/course.json", "section": "s2", "section_no": 2,
                                   "section_title": "数据从哪来", "screen": 2, "text": "标题太小", "images": [png]})).json()
            assert r["ok"] and r["num"] == s6.num
            num, text, imgs, via = b.sent[0]
            assert num == s6.num and "《淘宝 2003》第 2 节「数据从哪来」第 3 屏" in text and "标题太小" in text and len(imgs) == 1
            assert via == "feedback" and s6.ext["course"]["feedback"]["section"] == "s2"
            t = await (await c.get(f"/_x/api/thread/{s6.num}")).json()
            assert t["ext"]["course"]["feedback"]["screen"] == 2      # 改好后回复里的试课按钮跳回这一屏
            await post({"course": "/workspace/_drafts/c1/course.json", "section": "s2", "section_no": 2,
                        "section_title": "数据从哪来", "screen": 2, "step": 2, "steps": 4, "text": "字太小"})
            assert "第 3 屏的第 2 步（共 4 步）" in b.sent[1][1]
            await post({"course": "/workspace/_drafts/c1/course.json", "section": "s2", "section_no": 2,
                        "section_title": "数据从哪来", "screen": 0, "step": 0, "steps": 5, "step_name": "封面", "text": "y"})
            assert "第 1 屏的封面（共 5 步）" in b.sent[2][1]
            st = await (await c.get("/_x/ext/course/api/course?course=/workspace/_drafts/c1/course.json")).json()
            assert st["num"] == s6.num and st["state"] == "idle"
            s6.cwd = str(tmp_path / "别处")          # 没有在做这门课的对话 → 在课程根新开
            r = await (await post({"course": "/workspace/_drafts/c1/course.json", "text": "x"})).json()
            assert b.new and b.new[0][0] == [str(root)] and b.new[0][2] == "dev" and r["ok"]
            assert b.registry.get(r["num"]).ext["course"]["feedback"]["course"] == "/workspace/_drafts/c1/course.json"
            assert (await post({"course": "/etc/passwd", "text": "x"})).status == 400
            # 新对话“接着做某门课”：在课程根开，开场白说明是哪门课
            await c.post("/_x/api/send", json={"text": "封面改大", "course": "c1"})
            assert b.new[-1] == ([str(root)], "（这件事是关于课程《淘宝 2003》，目录 _drafts/c1/）\n封面改大", "dev")
        finally:
            await c.close()

    asyncio.run(go())


def test_restricted_account_sees_nothing(tmp_path):
    """受限账号：拿不到课、用不了扩展的地址、主页面不加载扩展脚本、试课预览也不转。"""
    async def go():
        b, root = _bridge(tmp_path)
        _course(root, "_drafts/c1")
        cu = await _login(b, "user", "demo-guest-test")
        try:
            assert (await (await cu.get("/_x/api/sessions")).json())["ext"] == {}
            assert "ext/course" not in await (await cu.get("/_x/")).text()
            for path in ("/_x/ext/course/api/course", "/_x/ext/course/static/course.js", "/studio-preview.html"):
                assert (await cu.get(path)).status == 403, path
            assert (await cu.post("/_x/ext/course/api/feedback", json={})).status == 403
        finally:
            await cu.close()

    asyncio.run(go())


def test_old_feedback_moves_into_ext(tmp_path):
    """以前存在对话 feedback 字段里的“反馈的那一屏”，加载扩展时挪进扩展数据；接力的新对话也带着。"""
    data = tmp_path / "data"
    data.mkdir()
    spot = {"course": "/workspace/_drafts/c1/course.json", "section": "s1", "screen": 0, "t": 1}
    (data / "sessions.json").write_text(json.dumps({"next_num": 2, "sessions": {"1": {"num": 1, "name": "做课", "cwd": "/x", "feedback": spot}}}))
    b, _ = _bridge(tmp_path)
    assert b.registry.get(1).ext == {"course": {"feedback": spot}}
    s2 = asyncio.run(b.relays.open(b.registry.get(1), "交接"))
    assert s2.ext == {"course": {"feedback": spot}} and s2.ext is not b.registry.get(1).ext
