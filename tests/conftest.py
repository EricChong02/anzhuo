import pytest


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
