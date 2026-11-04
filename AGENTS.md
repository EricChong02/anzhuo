# 小E（remote-claude）项目规则

- 用途与用法见 README；设计、分期与已定决定见 docs/design.md（改行为前先对照）。
- 不告诉被调用的 Claude “消息来自远程 / 钉钉桥”；给它的固定要求在 `sessions.APPEND`，只写要求不写原因。
- 安全不可放松：只认 `allowed_user_ids` 的单聊；密钥只走钥匙串；日志不记消息内容（原文只在本机 `recent.jsonl` 留最近 100 行，2026-10-25 庄毅同意，排查用）；转来的、引用的内容标成“参考资料”。
- 测试：`.venv/bin/pytest -q`。改了 Claude 运行方式，还要真跑一轮对话联调，单测通过不代表能用。
- 钉钉已停用（2026-11-04，配置 `dingtalk = false`），只用网页；钉钉代码暂留，后续单独清理。
- 网页有两个账号（配置 `[accounts.*]`，密码指纹在钥匙串，`scripts/set_account.py` 设置）：admin 全部权限；user 只在 `~/remote-window` 里干活（沙箱 + 路径钩子 + 不读全局设置），两边看不到对方的对话；user 每周最多用主账号周额度的 1/5（`usage.jsonl` 的标价折算金额 × 换算比例，比例的分母是本机全部 Claude Code 的消耗，见 `localspend.py`），和主账号同时重置。改 `USER_ROUTES`、`Runner._fence*` 前想清楚，别让 user 摸到外面。
- 改完代码重启常驻进程：`scripts/install_launchd.sh`。

## 改小E 本身

只改网页界面（`src/remote_claude/static/app.html`）不用重启：每次打开都重新读这个文件，刷新就生效。改 Python 才要按下面的办法重启。

在钉钉里改本仓库时，跑着的对话就在小E 里面：不要直接跑 `scripts/install_launchd.sh` 或重启小E，会把自己打断。改完用 `scripts/restart_when_idle.py` 在后台等所有对话空闲再重启（用 `start_new_session` 脱离当前进程）。

## 网页（第二期）

外网只经阿里云 nginx + SSH 反向隧道进来，登录在 `web.py`（一次性链接换 30 天 Cookie）。改试课放行名单（`preview_allowed`）前想清楚：本机 8000 连着真名开发库，教师台、学生、班级相关的接口和 WebSocket 不能放出去。
