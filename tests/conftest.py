import pytest

try:   # 分派员只在私有版；开源导出不带，用到它的测试各自 importorskip
    from remote_claude import router as _router
except ImportError:
    _router = None

_REAL_CLASSIFY_KIND = _router and _router.classify_kind   # 下面换成假的之前留一份，测它本身时用


@pytest.fixture
def real_classify_kind():
    return _REAL_CLASSIFY_KIND


@pytest.fixture(autouse=True)
def _no_auto_title(monkeypatch):
    """测试里不真的调模型起名字。"""
    from remote_claude import naming

    async def fake(asked, answer, timeout=60):
        return ""
    monkeypatch.setattr(naming, "short_title", fake)


@pytest.fixture(autouse=True)
def _no_classify(monkeypatch):
    """测试里不真的调模型判断任务档位（判断不出来就按老规矩：代码仓算开发、别处算简单任务）。"""
    async def fake(text, timeout=15):
        return None
    if _router:
        monkeypatch.setattr(_router, "classify_kind", fake)


@pytest.fixture(autouse=True)
def _no_deepseek_key(monkeypatch):
    """测试里不读钥匙串、不真的连 DeepSeek（辅助步骤失败时保留原文或沿用本地规则）。"""
    from remote_claude import deepseek_api
    monkeypatch.setattr(deepseek_api, "key", lambda: None)


@pytest.fixture(autouse=True)
def _isolated_helper_usage(tmp_path, monkeypatch):
    from remote_claude import helper_usage
    monkeypatch.setattr(helper_usage, 'PATH', tmp_path / 'helper_usage.jsonl')


@pytest.fixture(autouse=True)
def _no_live_gpt_quota(monkeypatch):
    """自动分流可读取额度，测试默认不启动真实 Codex；特定用例自行覆盖。"""
    from remote_claude import codex

    async def fake():
        return {}
    monkeypatch.setattr(codex, "rate_limits", fake)
