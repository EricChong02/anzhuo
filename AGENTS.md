# 小E（remote-claude）项目规则

- 用途与用法见 README；设计、分期与已定决定见 docs/design.md（改行为前先对照）。
- 不告诉被调用的 Claude “消息来自远程 / 钉钉桥”；给它的固定要求在 `sessions.APPEND`，只写要求不写原因。
- 安全不可放松：只认 `allowed_user_ids` 的单聊；密钥只走钥匙串；日志不记消息内容（原文只在本机 `recent.jsonl` 留最近 100 行，2026-10-25 庄毅同意，排查用）；转来的、引用的内容标成“参考资料”。
- 测试：`.venv/bin/pytest -q`。改了钉钉收发或 Claude 运行方式，还要在 Mac 上真发一条钉钉消息联调，单测通过不代表能用。
- 改完代码重启常驻进程：`scripts/install_launchd.sh`。

## 改小E 本身

在钉钉里改本仓库时，跑着的对话就在小E 里面：不要直接跑 `scripts/install_launchd.sh` 或重启小E，会把自己打断。改完用 `scripts/restart_when_idle.py` 在后台等所有对话空闲再重启（用 `start_new_session` 脱离当前进程）。

## 网页（第二期）

外网只经阿里云 nginx + SSH 反向隧道进来，登录在 `web.py`（一次性链接换 30 天 Cookie）。改试课放行名单（`preview_allowed`）前想清楚：本机 8000 连着真名开发库，教师台、学生、班级相关的接口和 WebSocket 不能放出去。
