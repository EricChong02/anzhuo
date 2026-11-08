import pytest

from remote_claude import router as _router

_REAL_CLASSIFY_KIND = _router.classify_kind   # 下面换成假的之前留一份，测它本身时用


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
    from remote_claude import router

    async def fake(text, timeout=15):
        return None
    monkeypatch.setattr(router, "classify_kind", fake)


@pytest.fixture(autouse=True)
def _no_deepseek_key(monkeypatch):
    """测试里不读钥匙串、不真的连 DeepSeek（翻译、任务分组、判断档位都会退回 Haiku 那条路）。"""
    from remote_claude import deepseek_api
    monkeypatch.setattr(deepseek_api, "key", lambda: None)
