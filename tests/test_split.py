"""分工表（split.py）：老写法兼容、首选用不了换备选、DeepSeek 禁区、受限账号、点名了哪家，以及设置页存回 config.toml。"""
from pathlib import Path

import pytest

from anzhuo import prefs, split
from anzhuo.config import Config, load_config
from anzhuo.engines import CODEX, DEEPSEEK, MODELS


@pytest.fixture
def both(monkeypatch):
    """Codex 装了、DeepSeek 有密钥。"""
    monkeypatch.setattr(type(CODEX), "available", lambda self: True)
    monkeypatch.setattr(type(DEEPSEEK), "available", lambda self: True)


def cfg(**rows) -> Config:
    return Config(split=split.normalize(rows))


def test_normalize_and_legacy():
    t = split.normalize(None)
    assert t["design"]["use"] == "opus" and t["dev"]["use"] == "sonnet" and t["simple"]["use"] == "sonnet"
    assert split.normalize(None, {"dev_model": "haiku"})["dev"]["use"] == "haiku"           # 老写法照认
    assert split.normalize({"dev": "gpt"}, {"dev_model": "haiku"})["dev"]["use"] == "gpt"    # 新写法优先，简写也认
    assert split.normalize({"dev": {"model": "x"}})["dev"] == {"use": "sonnet", "model": "x", "effort": "", "backup": ""}


def test_load_config_reads_split(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('design_model = "fable"\n[split.dev]\nuse = "gpt"\nmodel = "gpt-5.5-terra"\nbackup = "sonnet"\n', encoding="utf-8")
    c = load_config(p)
    assert c.split["design"]["use"] == "fable"
    assert c.split["dev"] == {"use": "gpt", "model": "gpt-5.5-terra", "effort": "", "backup": "sonnet"}


def test_choose_primary_backup_fallback(both, monkeypatch):
    c = cfg(dev={"use": "gpt", "model": "gpt-5.5-terra", "effort": "high", "backup": "sonnet"})
    got = split.choose(c, "dev", ["/tmp"])
    assert (got.pick, got.engine, got.full_model, got.effort, got.note) == ("gpt", "codex", "gpt-5.5-terra", "high", "")
    monkeypatch.setattr(type(CODEX), "available", lambda self: False)   # Codex 没装：换备选，并说清为什么
    got = split.choose(c, "dev", ["/tmp"])
    assert got.pick == "sonnet" and got.full_model == MODELS["sonnet"] and "没装" in got.note and "备选" in got.note
    got = split.choose(cfg(dev={"use": "gpt"}), "dev", ["/tmp"])          # 没设备选：退回 Sonnet
    assert got.pick == "sonnet" and "备选" not in got.note
    assert split.choose(c, None).pick == "sonnet"                         # 档位不明当简单任务


def test_choose_respects_deepseek_block_and_fenced(both):
    c = Config(split=split.normalize({"simple": {"use": "deepseek", "backup": "haiku"}}), deepseek_block=["私密资料"])
    assert split.choose(c, "simple", ["/x/notes"]).pick == "deepseek"
    got = split.choose(c, "simple", ["/x/私密资料/日记"])
    assert got.pick == "haiku" and "私密资料" in got.note
    assert split.choose(c, "simple", ["/x/notes"], fenced=True).pick == "haiku"   # 受限账号只用 Claude


def test_choose_within_named_engine(both):
    c = cfg(dev={"use": "gpt", "backup": "opus"})
    assert split.choose(c, "dev", engine="claude").pick == "opus"     # 点名 Claude：只在 Claude 里挑（用备选里的 Claude）
    assert split.choose(cfg(dev={"use": "gpt"}), "dev", engine="claude").pick == "sonnet"
    assert split.choose(cfg(dev={"use": "sonnet"}), "dev", engine="deepseek").pick == "deepseek"


def test_choose_by_quota(both):
    """质量优先：首选这家偏紧照用；告急 / 撞线才换备选，备选得更宽裕（或同是 Claude 换更省的型号）。"""
    c = cfg(dev={"use": "gpt", "backup": "sonnet"}, design={"use": "opus", "backup": "sonnet"})
    assert split.choose(c, "dev", levels={"gpt": "偏紧"}).pick == "gpt"
    got = split.choose(c, "dev", levels={"gpt": "告急"})
    assert got.pick == "sonnet" and "额度告急" in got.note
    assert split.choose(c, "dev", levels={"gpt": "告急", "claude": "告急"}).pick == "gpt"     # 备选那家也一样紧：不换
    assert split.choose(c, "dev", levels={"gpt": "撞线", "claude": "撞线"}).pick == "gpt"
    assert split.choose(c, "design", levels={"claude": "告急"}).pick == "sonnet"            # 同是 Claude：换更省的
    assert split.choose(c, "design", levels={"claude": "撞线"}).pick == "opus"              # 同家撞线换了也没用
    assert split.choose(cfg(dev={"use": "sonnet", "backup": "opus"}), "dev", levels={"claude": "告急"}).pick == "sonnet"
    got = split.choose(cfg(simple={"use": "gpt"}), "simple", levels={"gpt": "撞线"})      # 没备选、别家撞线：退回 Sonnet
    assert got.pick == "sonnet" and "撞线" in got.note
    assert split.choose(c, "dev", engine="gpt", levels={"gpt": "撞线"}).pick == "gpt"     # 点名了哪家：不看额度


def test_check():
    ok = split.normalize({"dev": {"use": "gpt", "backup": "sonnet", "effort": "high"}})
    assert split.check(ok) == ""
    assert "首选" in split.check({"dev": {"use": "gpt-9"}})
    assert "备选" in split.check({"dev": {"use": "gpt", "backup": "nope"}})
    assert "不用另填型号" in split.check({"dev": {"use": "opus", "model": "x"}})
    assert "档位" in split.check({"other": {"use": "opus"}})


def test_describe():
    s = split.describe(cfg(dev={"use": "gpt", "backup": "sonnet"}))
    assert "定方案用 opus" in s and "开发用 gpt（备选 sonnet）" in s


def test_prefs_saves_split_and_drops_legacy(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('dev_model = "sonnet"  # 老写法\nreply_language = "简体中文"\n', encoding="utf-8")
    cur = load_config(p)
    changes = prefs.parse({"split": {"dev": {"use": "gpt", "model": "", "effort": "", "backup": "sonnet"}}}, cur)
    assert changes["split"]["dev"]["use"] == "gpt" and changes["split"]["design"]["use"] == "opus"
    prefs.save(p, changes)
    text = p.read_text(encoding="utf-8")
    assert "dev_model" not in text and "[split.dev]" in text and 'reply_language = "简体中文"' in text
    assert 'model = ""' not in text                                       # 空的项不写
    assert load_config(p).split["dev"] == {"use": "gpt", "model": "", "effort": "", "backup": "sonnet"}
    with pytest.raises(prefs.Invalid, match="分工表"):
        prefs.parse({"split": {"dev": {"use": "opus", "model": "gpt-5"}}}, cur)
    f = next(x for x in prefs.schema() if x["key"] == "split")
    assert [r[0] for r in f["rows"]] == ["design", "dev", "simple"] and ["gpt", "GPT（Codex）"] in f["picks"]
    assert "deepseek" in f["engine_models"]
    assert Path(p).exists()


def test_new_session_follows_split(both, tmp_path, monkeypatch):
    """新开对话没点名：照分工表换到别家，开工卡片写明是分工表定的；点名了就照你说的。"""
    import asyncio
    router = pytest.importorskip("anzhuo.router")
    from anzhuo.app import Bridge
    from anzhuo.dingtalk import Incoming
    from anzhuo.sessions import Runner
    from test_bridge import FakeDT

    async def classify(text, timeout=15):
        return "dev"
    monkeypatch.setattr(router, "classify_kind", classify)
    submitted = []

    async def fake_submit(self, s, text, inc, opening=""):
        submitted.append(opening)
    monkeypatch.setattr(Bridge, "submit", fake_submit)
    monkeypatch.setattr(Runner, "_run", lambda *a, **k: None)
    (tmp_path / "w").mkdir()
    c = Config(scratch_root=str(tmp_path / "s"),
               split=split.normalize({"dev": {"use": "gpt", "model": "gpt-5.5-terra", "effort": "high", "backup": "sonnet"}}))
    inc = lambda t: Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text=t, via="web")

    async def go():
        b = Bridge(c, FakeDT(), data_dir=tmp_path / "data")
        n = b.registry.reserve_num()
        await b.start_session(n, tmp_path / "w", [], "修个 bug", inc("修个 bug"))
        s = b.registry.get(n)
        assert (s.engine, s.model, s.effort) == ("codex", "gpt-5.5-terra", "high")
        assert "分工表 · 开发" in submitted[-1]
        n = b.registry.reserve_num()
        await b.start_session(n, tmp_path / "w", [], "修个 bug 用 opus", inc("修个 bug 用 opus"))
        s = b.registry.get(n)
        assert (s.engine, s.model) == ("claude", MODELS["opus"]) and "你指定的" in submitted[-1]
        n = b.registry.reserve_num()   # 受限账号：只用 Claude，换成备选
        await b.start_session(n, tmp_path / "w", [], "修个 bug", inc("修个 bug"), owner="g", root=str(tmp_path / "w"))
        s = b.registry.get(n)
        assert (s.engine, s.model) == ("claude", MODELS["sonnet"])
    asyncio.run(go())


def test_subagent_model_options_show_full_names():
    from anzhuo import prefs
    opts = dict(prefs._options(prefs.BY_KEY["subagent_model"]))
    assert opts[""] == "跟主对话" and opts["sonnet"].startswith("Claude Sonnet ")
