"""子路径挂载（web_prefix）：整站挂在 {web_prefix}/... 下，供宿主面板反代透传；空前缀 = 现状不变。"""
import asyncio
import re
import time

from aiohttp.test_utils import TestClient, TestServer

from anzhuo.config import Config, load_config
from anzhuo.web import Web, hash_password
from anzhuo.webui import app_html


def _web(tmp_path, prefix=""):
    return Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("pw", iters=1000), web_prefix=prefix)


def test_config_web_prefix_normalization(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('web_base = "http://127.0.0.1:8765"\n')
    assert load_config(p).web_prefix == ""                       # 不配 = 空，现状不变
    p.write_text('web_prefix = "/yuanchuang"\n')
    assert load_config(p).web_prefix == "/yuanchuang"
    p.write_text('web_prefix = "yuanchuang/"\n')
    assert load_config(p).web_prefix == "/yuanchuang"            # 补上开头的 /、去掉结尾的 /


def test_app_html_prefix_replacement():
    bare = app_html()
    assert '"/_x/' in bare and "'/_x/" in bare                   # 页面里本来就有根绝对引用
    tagged = app_html("/yuanchuang")
    assert '"/_x/' not in tagged and "'/_x/" not in tagged and "`/_x/" not in tagged
    assert '"/yuanchuang/_x/' in tagged and "'/yuanchuang/_x/" in tagged
    assert app_html("") == bare


def test_empty_prefix_regression(tmp_path):
    """空前缀：路由、重定向与以前完全一样。"""

    async def go():
        w = _web(tmp_path)
        assert w.px == "/_x"
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            assert (await c.get("/_x/health")).status == 200
            assert (await c.get("/_x/login", allow_redirects=False)).status == 200
            assert "/_x/login" in (await c.get("/_x/t/nope", allow_redirects=False)).headers["Location"]
            # 登录后根路径跳主界面（现状）
            r = await c.post("/_x/login", data={"username": "u", "password": "pw"})
            r = await c.get("/", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/_x/"
        finally:
            await c.close()

    asyncio.run(go())


def test_prefixed_routes_and_redirects(tmp_path):
    """web_prefix = "/yuanchuang"：整站在 /yuanchuang 下可达，重定向都带前缀。"""

    async def go():
        w = _web(tmp_path, "/yuanchuang")
        assert w.px == "/yuanchuang/_x"
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            # 未登录：公开资源在带前缀的路径上可达
            assert (await c.get("/yuanchuang/_x/health")).status == 200
            # 没登录访问受限页：跳到带前缀的登录页；接口则 401
            r = await c.get("/yuanchuang/_x/history", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"].startswith("/yuanchuang/_x/login?next=")
            assert (await c.get("/yuanchuang/_x/api/events")).status == 401   # SSE 路由注册在前缀下
            assert (await c.get("/yuanchuang/_x/api/send", data={})).status in (400, 401)
            # 登录后：页面、资源、文件下载都在前缀下
            r = await c.post("/yuanchuang/_x/login", data={"username": "u", "password": "pw"}, allow_redirects=False)
            assert r.status == 302
            page = await (await c.get("/yuanchuang/_x/")).text()
            assert re.search(r"(?<!/yuanchuang)/_x/", page) is None          # 渲染产物里没有裸的 /_x 引用
            sw = await (await c.get("/yuanchuang/_x/sw.js")).text()
            assert re.search(r"(?<!/yuanchuang)/_x/", sw) is None            # sw.js 里的引用也带了前缀
            manifest = await (await c.get("/yuanchuang/_x/manifest.json")).json()
            assert manifest["start_url"] == "/yuanchuang/_x/" and manifest["icons"][0]["src"] == "/yuanchuang/_x/icon.png"
            assert (await c.get("/yuanchuang/_x/icon.png")).status == 200
            assert (await c.get("/yuanchuang/dl/x/1", allow_redirects=False)).status in (302, 404)  # /dl 搬到了前缀下
            # 登录后：根路径 → 挂载点 → 主界面，逐级友好跳转
            r = await c.get("/", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/yuanchuang/"
            r = await c.get("/yuanchuang/", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/yuanchuang/_x/"
            r = await c.get("/yuanchuang/_x", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/yuanchuang/_x/"
            # 老的根路径 /_x/* 不再可达：不占用宿主的命名空间
            assert (await c.get("/_x/health", allow_redirects=False)).status == 404
            # 登录跳转只认本站路径
            r = await c.get("/yuanchuang/_x/login?next=//evil.example/x", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/yuanchuang/_x/"   # 不认外站地址，落回主界面
        finally:
            await c.close()

    asyncio.run(go())


def test_prefixed_login_flow(tmp_path):
    """登录、登出与跳转都走带前缀的地址。"""

    async def go():
        w = _web(tmp_path, "/panel")
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            r = await c.get("/panel/_x/history", allow_redirects=False)
            login_path = r.headers["Location"].split("?")[0]
            assert login_path == "/panel/_x/login"
            login = await (await c.get(r.headers["Location"])).text()
            assert 'action="/panel/_x/login"' in login                      # 表单提交地址带前缀
            r = await c.post("/panel/_x/login", data={"username": "u", "password": "pw", "next": "/panel/_x/"},
                             allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/panel/_x/"
            assert (await c.get("/panel/_x/history")).status == 200
            # 挂载点无尾斜杠：已登录（守卫放行）必须 302 到主界面，不能 404
            r = await c.get("/panel", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/panel/_x/"
            r = await c.post("/panel/_x/api/logout", json={"all": True}, allow_redirects=False)
            assert r.status == 200
        finally:
            await c.close()

    asyncio.run(go())


def test_turn_records_carry_prefix(tmp_path):
    """过程记录里的截图和文件地址按前缀生成。"""
    from anzhuo.report import Turn

    async def go():
        w = _web(tmp_path, "/yuanchuang")
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        f = tmp_path / "out.txt"
        f.write_text("hi", encoding="utf-8")
        t = Turn(started=time.time() - 60, ended=time.time(), request="测试", final_text="好了",
                 images=[str(img)], files=[str(f)], timeline=[])
        tid = w.save_turn("#1 测试", t, str(tmp_path), "做完了")
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/yuanchuang/_x/login", data={"username": "u", "password": "pw"})
            page = await (await c.get(f"/yuanchuang/_x/t/{tid}")).text()
            assert "/yuanchuang/_x/t/" in page and re.search(r"(?<!/yuanchuang)/_x/", page) is None
            assert (await c.get(f"/yuanchuang/_x/t/{tid}/img/0")).status == 200
            assert (await c.get(f"/yuanchuang/dl/{tid}/0")).status == 200
        finally:
            await c.close()
        # 非管理员也能走带前缀的 /dl（user_routes 平移要覆盖 /dl，不然这里 403）
        w2 = Web("http://x.example", 0, tmp_path, web_prefix="/yuanchuang",
                 accounts={"u": {"role": "admin", "root": "", "hash": hash_password("pw", iters=1000)},
                           "v": {"role": "user", "root": str(tmp_path), "hash": hash_password("pw2", iters=1000)}})
        c2 = TestClient(TestServer(w2.app()))
        await c2.start_server()
        try:
            await c2.post("/yuanchuang/_x/login", data={"username": "v", "password": "pw2"})
            assert (await c2.get(f"/yuanchuang/dl/{tid}/0")).status == 200
        finally:
            await c2.close()

    asyncio.run(go())
