"""远窗网页对话框（单页）：页面本身在 static/app.html，每次打开都重新读——只改界面时不用重启远窗，刷新就生效。
界面取向：中性底 + 一个强调蓝、状态只用小圆点、不用左侧/顶部色条、不弹成功提示（进展写在按钮自己身上）、不放难看的小字。"""
from pathlib import Path

APP_PATH = Path(__file__).parent / "static" / "app.html"


SETTINGS_PATH = Path(__file__).parent / "static" / "settings.js"   # 管理 · 设置页的脚本（打开那一页才加载）


def with_prefix(text: str, prefix: str = "") -> str:
    """挂了子路径（web_prefix）时，页面里所有根绝对引用 "/_x/…" 都要带上前缀，
    浏览器才会把请求发给本应用而不是宿主面板的根（fetch / EventSource / SW 登记 / 图标 / 音频全在这）。
    单引号、双引号、模板字符串三种写法都要换。"""
    if prefix:
        for quote in ('"', "'", "`"):
            text = text.replace(quote + "/_x/", quote + prefix + "/_x/")
    return text


def app_html(prefix: str = "") -> str:
    """单文件前端。"""
    return with_prefix(APP_PATH.read_text(), prefix)


def settings_js(prefix: str = "") -> str:
    return with_prefix(SETTINGS_PATH.read_text(encoding="utf-8"), prefix)
