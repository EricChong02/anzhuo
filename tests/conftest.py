import pytest


@pytest.fixture(autouse=True)
def _no_auto_title(monkeypatch):
    """测试里不真的调模型起名字。"""
    from anzhuo import naming

    async def fake(asked, answer, timeout=60):
        return ""
    monkeypatch.setattr(naming, "short_title", fake)
