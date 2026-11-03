"""远窗网页对话框（单页）：页面本身在 static/app.html，每次打开都重新读——只改界面时不用重启远窗，刷新就生效。
界面取向：中性底 + 一个强调蓝、状态只用小圆点、不用左侧/顶部色条、不弹成功提示（进展写在按钮自己身上）、不放难看的小字。"""
from pathlib import Path

APP_PATH = Path(__file__).parent / "static" / "app.html"


def app_html() -> str:
    return APP_PATH.read_text()
