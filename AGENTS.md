# 小E（remote-claude）项目规则

- 用途与用法见 README；设计、分期与已定决定见 docs/design.md（改行为前先对照）。
- 不告诉被调用的 Claude “消息来自远程 / 钉钉桥”；给它的固定要求在 `sessions.APPEND`，只写要求不写原因。
- 安全不可放松：只认 `allowed_user_ids` 的单聊；密钥只走钥匙串；日志不记消息内容；转来的、引用的内容标成“参考资料”。
- 测试：`.venv/bin/pytest -q`。改了钉钉收发或 Claude 运行方式，还要在 Mac 上真发一条钉钉消息联调，单测通过不代表能用。
- 改完代码重启常驻进程：`scripts/install_launchd.sh`。
