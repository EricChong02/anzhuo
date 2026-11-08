"""开源版的底线：没有钉钉也能跑、默认配置保守、不带任何个人信息、没有做课/自动判断的残留。"""
import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path

import pytest

from anzhuo.app import Bridge
from anzhuo.commands import Cmd
from anzhuo.config import Config, load_config
from anzhuo.dingtalk import Incoming, NoDingTalk

ROOT = Path(__file__).resolve().parents[1]


def test_config_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("")
    c = load_config(p)
    assert not c.dingtalk_enabled                      # 不填 client_id = 只用网页
    assert c.web_base == "http://127.0.0.1:8765"       # 网页默认开在本机
    assert (c.auto_review, c.auto_cleanup) == (False, False)   # 危险命令等人点；不自动归档、清理
    assert not c.task_grouping and c.accounts == {}
    assert not c.super_agent                           # 总管默认不开
    assert c.skip_names == [] and c.folders == {} and c.deepseek_block == [] and c.extra_instructions == "" and c.review_policy == ""
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
    p = Path(tempfile.mkdtemp()) / "c.toml"
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
    from anzhuo.engines.base import codex_instructions
    from anzhuo.engines.claude import build_instructions
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
        await b.message(Cmd("message", text="你好"), inc)       # 还没有对话：提示先开一个，不崩
        await b.new_session([], "查一下天气", inc)
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError):
            await b.dt.download("code", tmp_path / "x")
    asyncio.run(go())
    assert seen == [(1, "查一下天气")]


def test_no_course_or_router_leftovers():
    html = "\n".join(p.read_text(encoding="utf-8") for p in [ROOT / "src/anzhuo/static/app.html", *(ROOT / "src/anzhuo/static/app").iterdir()])
    for word in ("试课", "课程根", "studio-preview", "feedback.js", "drafts"):
        assert word not in html, word
    assert not (ROOT / "src/anzhuo/router.py").exists() and not (ROOT / "src/anzhuo/extensions/course").exists()
    assert not (ROOT / "tests/test_ext_course.py").exists()
    web = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src/anzhuo/web").glob("*.py"))
    assert "_proxy" not in web and "preview" not in web      # 兜底路由只给扩展；导出包没有扩展，没登记的地址一律 404


# 不该出现在开源导出里的个人信息（域名、服务器、人名、内部项目名）：只存 sha256 前 16 位和字数，仓库里不留明文。
# 想补新词：在本机 ~/.config/anzhuo/leak_words.txt 里一行写一个（只在本机，不进仓库）。
LEAK_HASHES = {
    (2, "929bbff0471488d7"),
    (3, "2c087d5f7f118309"),
    (3, "b5fa3668564f7eb3"),
    (6, "156430ed7a35c692"),
    (7, "2519d82bccdbe631"),
    (7, "2a08b963cd3ddb65"),
    (8, "052c489bdb1f6942"),
    (10, "1ee773c5a0fc85ff"),
    (10, "d588e00690fab286"),
    (11, "56837fce5278c9c3"),
}
LOCAL_WORDS = Path.home() / ".config" / "anzhuo" / "leak_words.txt"
SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".lock", ".pyc", ".mp3", ".mp4", ".woff", ".woff2")


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _leaks(text: str, hashes: set, words: list[str]) -> list[str]:
    """按名单里的字数挨个截片段算指纹比对；命中的只报“第几个字起、多长”，不回显原文。"""
    hits = []
    for n in sorted({n for n, _ in hashes}):
        want = {h for m, h in hashes if m == n}
        for i in range(len(text) - n + 1):
            if (h := _fp(text[i:i + n])) in want:
                hits.append(f"第{i}字起{n}字")
                want.discard(h)        # 每个词报一次就够
                if not want:
                    break
    hits += [f"本机词表第{k + 1}个" for k, w in enumerate(words) if w and w in text]
    return hits


def test_leak_check_works():
    """自检：算法能认出名单里的词（用一个临时词，不用真名单）。"""
    assert _leaks("abc密钥xyz", {(2, _fp("密钥"))}, []) == ["第3字起2字"]
    assert _leaks("干净的文本", {(2, _fp("密钥"))}, ["没有"]) == []


def test_no_personal_traces():
    """仓库里不留作者个人的域名、服务器、人名、内部项目名。"""
    words = [w.strip() for w in LOCAL_WORDS.read_text(encoding="utf-8").splitlines()] if LOCAL_WORDS.exists() else []
    hits = []
    for f in ROOT.rglob("*"):
        if (not f.is_file() or f.suffix.lower() in SKIP_SUFFIX or ".git" in f.parts or ".venv" in f.parts
                or any(d in f.parts for d in ("__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"))
                or f.resolve() == Path(__file__).resolve()):
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{f.relative_to(ROOT)}: {h}" for h in _leaks(text, LEAK_HASHES, words)]
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
