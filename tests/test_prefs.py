"""网页设置（prefs.py）：校验、写回 config.toml（保留注释）、马上生效 / 重启生效、只有管理员能改、子路径下也能用。"""
import asyncio
import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from anzhuo import prefs
from anzhuo.app import Bridge
from anzhuo.config import load_config
from anzhuo.web import Web, hash_password

from test_bridge import FakeDT

TOML = '''web_user = "u"  # 登录账号
# 总管
super_agent = false
reply_language = "简体中文"
extra_instructions = "小改动直接提交"   # 我自己的要求

[folders]
"工作" = "{work}"
'''


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "work").mkdir()
    path = tmp_path / "config.toml"
    path.write_text(TOML.format(work=str(tmp_path / "work").replace("\\", "/")), encoding="utf-8")
    b = Bridge(load_config(path), FakeDT(), data_dir=tmp_path / "data")
    return path, b


def test_parse_validates(setup):
    path, b = setup
    cur = load_config(path)
    assert prefs.parse({"reply_language": "简体中文"}, cur) == {}                    # 没变的不算
    assert prefs.parse({"max_parallel": "4", "nope": 1}, cur) == {"max_parallel": 4}   # 不认得的忽略
    for bad, word in [({"max_parallel": 99}, "1 到 10"), ({"split": {"dev": {"use": "gpt-9"}}}, "可选范围"),
                      ({"auto_review": "yes"}, "开要么关"), ({"timezone": "Mars/Base"}, "时区"),
                      ({"folders": "没有等号"}, "短名 = 路径"), ({"folders": "x = /no/such/dir"}, "不存在"),
                      ({"super_agent_name": "名" * 31}, "最多 30")]:
        with pytest.raises(prefs.Invalid, match=word):
            prefs.parse(bad, cur)
    assert prefs.parse({"search_roots": "~/a\n\n ~/b "}, cur) == {"search_roots": ["~/a", "~/b"]}


def test_save_keeps_comments_and_other_keys(setup):
    path, b = setup
    r = prefs.update(b, {"super_agent_routing": "写代码：claude\n查资料：kimi", "reply_language": "English"}, path)
    assert r["ok"] and set(r["changed"]) == {"例外的分派规则", "回复语言"}
    text = path.read_text(encoding="utf-8")
    assert "# 我自己的要求" in text and "# 总管" in text and 'web_user = "u"' in text   # 注释、别的项都还在
    cfg = load_config(path)
    assert cfg.super_agent_routing.strip() == "写代码：claude\n查资料：kimi" and cfg.reply_language == "English"
    assert cfg.folders == {"工作": str(path.parent / "work").replace("\\", "/")}
    prefs.update(b, {"extra_instructions": ""}, path)                                 # 改回默认：那一行删掉
    assert "extra_instructions" not in path.read_text(encoding="utf-8")


def test_live_and_restart(setup):
    path, b = setup
    r = prefs.update(b, {"reply_language": "English", "auto_review": True, "max_parallel": 5, "super_agent": True}, path)
    assert b.runner.language == "English" and b.runner.auto_review and b.cfg.reply_language == "English"   # 马上生效
    assert b.cfg.max_parallel == 3 and b.cfg.super_agent is False                    # 要重启的：运行中不变
    assert set(r["restart"]) == {"最多同时干活的对话", "总管"}
    assert set(prefs.payload(b, path)["restart"]) == set(r["restart"])
    assert prefs.payload(b, path)["values"]["max_parallel"] == 5                     # 设置页显示文件里的新值


def test_router_rename_is_live(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("super_agent = true\n", encoding="utf-8")
    b = Bridge(load_config(path), FakeDT(), data_dir=tmp_path / "data")
    from anzhuo import superagent
    prefs.update(b, {"super_agent_name": "管家"}, path)
    assert superagent.find(b).name == "管家" and "你是“管家”" in superagent.role(b, superagent.find(b))["append"]


@pytest.mark.parametrize("prefix", ["", "/yuanchuang"])
def test_api_admin_only_and_prefix(setup, tmp_path, prefix):
    path, b = setup

    async def go():
        w = Web("http://x.example", 0, tmp_path / "w", accounts={
            "u": {"role": "admin", "root": "", "hash": hash_password("pw", iters=1000)},
            "g": {"role": "user", "root": str(tmp_path / "g"), "hash": hash_password("pw", iters=1000)}}, web_prefix=prefix)
        w.bridge, w.config_path = b, path
        px = prefix + "/_x"
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            assert (await c.get(f"{px}/api/admin/prefs")).status == 401                 # 没登录
            await c.post(f"{px}/login", data={"username": "g", "password": "pw"})
            assert (await c.get(f"{px}/api/admin/prefs")).status == 403                 # 受限账号不行
            assert (await c.get(f"{px}/admin/settings.js")).status == 403
            await c.post(f"{px}/api/logout")
            c.session.cookie_jar.clear()
            await c.post(f"{px}/login", data={"username": "u", "password": "pw"})
            d = await (await c.get(f"{px}/api/admin/prefs")).json()
            assert {f["key"] for f in d["fields"]} >= {"super_agent", "super_agent_routing", "folders"}
            r = await (await c.post(f"{px}/api/admin/prefs", json={"reply_language": "English"})).json()
            assert r["ok"] and b.runner.language == "English"
            r = await (await c.post(f"{px}/api/admin/prefs", json={"max_parallel": 0})).json()
            assert not r["ok"] and "1 到 10" in r["error"]
            js = await (await c.get(f"{px}/admin/settings.js")).text()
            assert "drawSettings" in js and re.search(r"(?<!" + re.escape(prefix) + r")/_x/", js) is None if prefix else "'/_x/api/admin/prefs'" in js
            page = await (await c.get(f"{px}/")).text()
            assert f"'{px}/admin/settings.js'" in page                                 # 页面里加载脚本的地址也带了前缀
        finally:
            await c.close()

    asyncio.run(go())


def test_chinese_folder_name_roundtrip(setup):
    path, b = setup
    work = str(path.parent / "work")
    assert prefs.update(b, {"folders": f"工作 = {work}\n笔记本 = {work}"}, path)["ok"]
    assert load_config(path).folders == {"工作": work, "笔记本": work}   # 写出来的 TOML 能再读回来（中文键加了引号）
    assert b.cfg.folders == {"工作": work, "笔记本": work}               # 马上生效
