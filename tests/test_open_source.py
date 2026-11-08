"""开源版的底线：没有钉钉也能跑、默认配置保守、不带任何个人信息、没有做课/自动判断的残留。"""
import asyncio
import re
from pathlib import Path

import pytest

from anzhuo.app import Bridge
from anzhuo.commands import Cmd
from anzhuo.config import Config, load_config
from anzhuo.dingtalk import Incoming, NoDingTalk

ROOT = Path(__file__).resolve().parent.parent


def test_config_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    c = load_config(p)
    assert not c.dingtalk_enabled                      # 不填 client_id = 只用网页
    assert c.web_base == "http://127.0.0.1:8765"       # 网页默认开在本机
    assert (c.auto_review, c.auto_cleanup) == (False, False)   # 危险命令等人点；不自动归档、清理
    assert not c.task_grouping and c.accounts == {}
    assert not c.super_agent                           # 总管默认不开
    assert c.skip_names == [] and c.folders == {} and c.extra_instructions == "" and c.review_policy == ""
    p.write_text('web = false\nclient_id = "abc"\nallowed_user_ids = ["u1"]\nweb_port = 9000\n')
    c = load_config(p)
    assert c.dingtalk_enabled and c.web_base == "" and c.allowed_user_ids == ["u1"]
    p.write_text("web_port = 9000\n")
    assert load_config(p).web_base == "http://127.0.0.1:9000"
    p.write_text('web_base = "https://a.example.com/"\n')
    assert load_config(p).web_base == "https://a.example.com"


def test_translation_is_off_by_default(monkeypatch):
    """默认不翻译：不会有任何内容被送去 DeepSeek 或 Haiku；打开后才判断英文段落。"""
    from anzhuo import translate
    assert load_config(ROOT / "config.example.toml").translate_english is False   # 样例里是注释掉的，读到的就是默认值
    p = ROOT / "tests" / "_tmp_cfg.toml"
    p.write_text('client_id = ""\n')
    try:
        assert load_config(p).translate_english is False
    finally:
        p.unlink()
    monkeypatch.setattr(translate, "enabled", False)
    assert translate.needs_translation("This is a long English paragraph that would otherwise be translated.") is False
    monkeypatch.setattr(translate, "enabled", True)
    assert translate.needs_translation("This is a long English paragraph that would otherwise be translated.") is True


def test_instructions_are_neutral():
    from anzhuo.sessions import build_instructions, codex_instructions
    for text in (build_instructions(), codex_instructions()):
        assert "要你定" in text and "A. " in text
        for personal in ("提交、推送、上线", "restart_when_idle", "已授权"):
            assert personal not in text            # 内置规则里不替用户预先授权任何事
    assert "mcp__anzhuo__update_plan" in build_instructions()
    assert build_instructions("English").startswith("回复一律用English")
    assert not build_instructions("").startswith("回复一律用")      # 语言写空 = 不指定
    mine = "小改动做完直接提交，不用问我。"
    assert mine in build_instructions(extra=mine) and mine in codex_instructions(extra=mine)


def test_reviewer_policy_is_opt_in():
    from anzhuo.reviewer import system_prompt
    base = system_prompt()
    assert "来自他自己的配置" not in base and "推送到远端、上线、部署：除非" in base   # 默认不放行推送、上线
    assert "推送到 dev 分支可以放行" in system_prompt("推送到 dev 分支可以放行")


def test_web_only_bridge(tmp_path, monkeypatch):
    """没配钉钉：所有“发给钉钉”的调用都是空操作，不报错；对话照常开、照常说话。"""
    from anzhuo.sessions import Runner
    cfg = Config(web_base="http://127.0.0.1:1", scratch_root=str(tmp_path / "s"))
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append((s.num, job.text))
        turn.final_text = "好"

    async def no_usage(self, force=False):
        pass

    monkeypatch.setattr(Runner, "_run", fake_run)
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)

    async def go():
        b = Bridge(cfg, NoDingTalk(), data_dir=tmp_path / "data")
        b.receipt_delay = 0
        assert b.user_id == "local" and not cfg.dingtalk_enabled
        await b.say("标题", "正文")                    # 没钉钉：只记下，不发
        inc = Incoming(sender_id="local", sender_nick="", conversation_type="1", kind="text", text="你好", via="web")
        await b._message(Cmd("message", text="你好"), inc)       # 还没有对话：提示先开一个，不崩
        await b._new([], "查一下天气", inc)
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError):
            await b.dt.download("code", tmp_path / "x")
    asyncio.run(go())
    assert seen == [(1, "查一下天气")]


def test_no_course_or_router_leftovers():
    html = (ROOT / "src/anzhuo/static/app.html").read_text(encoding="utf-8")
    for word in ("试课", "课程根", "studio-preview", "feedback.js", "drafts"):
        assert word not in html, word
    assert not (ROOT / "src/anzhuo/router.py").exists() and not (ROOT / "src/anzhuo/static/feedback.js").exists()
    web = (ROOT / "src/anzhuo/web.py").read_text(encoding="utf-8")
    assert "_proxy" not in web and "/{tail:.*}" not in web      # 没登记的地址一律 404，不转发给任何别的服务


def test_no_personal_traces():
    """仓库里不留作者个人的域名、服务器、人名、内部项目名（拆开写，免得这个文件自己命中）。"""
    bad = ["eric" + "lab", "zhuang" + "yi", "庄" + "毅", "Cyber" + "Scope", "47." + "107.", "yi_" + "official", "纪丽" + "丽", "管舒" + "怡",
           "xiao" + "e" + ".", "com." + "zhuang"]
    hits = []
    for f in ROOT.rglob("*"):
        if (not f.is_file() or f.suffix in (".png", ".pdf", ".lock", ".pyc") or ".git/" in str(f) or ".venv" in f.parts
                or any(d in f.parts for d in ("__pycache__", ".ruff_cache", ".pytest_cache")) or f.name in ("test_open_source.py", ".git")):
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{f.relative_to(ROOT)}: {b}" for b in bad if b in text]
    assert not hits, hits


def test_restricted_accounts_require_macos_and_root(monkeypatch):
    from anzhuo import config
    cfg = Config(accounts={"owner": {"role": "admin"}, "guest": {"role": "user", "root": "~/guest"}})
    monkeypatch.setattr(config, "read_web_hash", lambda name: "test-hash")
    monkeypatch.setattr(config.sys, "platform", "win32")
    with pytest.raises(ValueError, match="macOS"):
        config.web_accounts(cfg)
    cfg.accounts = {"owner": {"role": "admin"}}
    assert config.web_accounts(cfg)["owner"]["hash"] == "test-hash"
    cfg.accounts = {"guest": {"role": "user", "root": "~/guest"}}
    with pytest.raises(ValueError, match="admin"):
        config.web_accounts(cfg)
