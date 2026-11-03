# 给 AI 编程助手的说明

这个仓库是「远窗」：一个 Python（aiohttp + 原生 HTML）写的小程序，让人在手机或网页上给电脑里的 Claude Code / Codex 派活。改代码前先读 `README.md` 和 `docs/架构.md`。

- 测试：`.venv/bin/pytest -q`（Windows 是 `.venv\Scripts\python.exe -m pytest -q`；`test_checkpoints` 里两个用例依赖 POSIX 权限位 / 符号链接特权，在 Windows 上会失败，属平台限制）。全部是离线测试，不会连 Claude、钉钉或你的钥匙串。
- 想先看界面：`.venv/bin/python scripts/demo.py`（Windows：`.venv\Scripts\python.exe`），打开 http://127.0.0.1:8799 ，账号密码都是 demo。
- 网页界面只有一个文件 `src/anzhuo/static/app.html`，每次打开都重新读取：只改界面不用重启，刷新页面即生效；改 Python 才要重启。
- 不要在远窗自己的对话里直接 `launchctl kickstart -k` 重启远窗，会打断正在干活的对话；用 `scripts/restart_when_idle.py`。
- 安全底线不要放松：网页必须登录；写接口校验同源；密钥只放钥匙串（Windows 的网页密码指纹例外，放配置目录旁 `web_hash.json`）；日志不记消息内容；内置提示词不替用户预先授权任何事（提交、推送、部署等授权由用户写在配置的 `extra_instructions` / `review_policy` 里）。
- 提交前确认没有带进个人信息（域名、服务器地址、真实姓名、密钥）；`tests/test_open_source.py` 会检查一部分。
