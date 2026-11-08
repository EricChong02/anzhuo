"""远窗网页对话框（单页）：页面骨架在 static/app.html，样式和脚本按主题分在 static/app/ 下，
每次打开都重新读、按 APP_CSS / APP_JS 的顺序拼回一页——只改界面时不用重启远窗，刷新就生效。
脚本拼进同一个 <script>，所以各文件共用一个作用域（函数互相直接调用），顺序就是原来单文件里的先后。
样子按作者的界面口味：中性底 + 一个强调蓝、状态只用小圆点、不用左侧/顶部色条、不弹成功提示（进展写在按钮自己身上）、不放难看的小字。"""
from pathlib import Path

APP_PATH = Path(__file__).parent / "static" / "app.html"
APP_DIR = APP_PATH.parent / "app"
# 样式后面的覆盖前面的（theme 是几版视觉叠上去的），脚本里顶层的 let/const 要先定义再用：顺序不能随便换
APP_CSS = ("base.css", "theme.css", "admin.css", "list.css")
APP_JS = ("core.js",        # 小工具、图标、扩展挂接点 XE、api / upload、Markdown
          "quota.js",       # 额度条、接力按钮、用量弹层
          "list.js",        # 侧栏列表：任务分组、搜索、滑动删除、长按菜单、定时任务、提示条
          "picker.js",      # 输入框里的模型 / 深度选择
          "nav.js",         # 打开对话、返回、“我在看”、实时推送、手机通知
          "viewers.js",     # 页面里看文件、看大图
          "menu.js",        # “更多”菜单：登录的设备、退出
          "thread.js",      # 画一个对话：回复、过程、快捷回答、回退
          "speech.js",      # 朗读播放器
          "thread-load.js", # 复制、分叉、长时间没动静的提示、对话缓存与加载
          "composer.js",    # 输入框：草稿、附件、发送；“在哪做”菜单、选文件夹、接电脑端对话
          "admin.js",       # 管理页：用量、账号、设置入口
          "new.js",         # 新对话页
          "boot.js")        # 键盘让位、启动


SETTINGS_PATH = Path(__file__).parent / "static" / "settings.js"   # 管理 · 设置页的脚本（打开那一页才加载）


def with_prefix(text: str, prefix: str = "") -> str:
    """挂了子路径（web_prefix）时，页面里所有根绝对引用 "/_x/…" 都要带上前缀，
    浏览器才会把请求发给本应用而不是宿主面板的根（fetch / EventSource / SW 登记 / 图标 / 音频全在这）。
    单引号、双引号、模板字符串三种写法都要换。"""
    if prefix:
        for quote in ('"', "'", "`"):
            text = text.replace(quote + "/_x/", quote + prefix + "/_x/")
    return text


def _assemble() -> str:
    read = lambda name: (APP_DIR / name).read_text(encoding="utf-8")
    shell = APP_PATH.read_text(encoding="utf-8")
    return (shell.replace("/*@css*/", "".join(map(read, APP_CSS)).rstrip("\n"), 1)
                 .replace("//@js", "".join(map(read, APP_JS)).rstrip("\n"), 1))


def app_html(prefix: str = "", scripts: list[str] = ()) -> str:
    """单文件前端。scripts = 扩展的前端脚本地址（已带前缀），排在主脚本后面加载，用 window.XE 挂到界面上。"""
    text = with_prefix(_assemble(), prefix)
    if scripts:
        tags = "".join(f'<script src="{src}"></script>' for src in scripts)
        text = text.replace("</body>", tags + "</body>", 1) if "</body>" in text else text + tags
    return text


def settings_js(prefix: str = "") -> str:
    return with_prefix(SETTINGS_PATH.read_text(encoding="utf-8"), prefix)
