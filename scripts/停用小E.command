#!/bin/zsh
# 双击：立刻停掉小E，并取消开机自启。要恢复就运行 scripts/install_launchd.sh。
launchctl bootout "gui/$(id -u)/com.zhuangyi.remote-claude" 2>/dev/null && echo "小E 已停用。" || echo "小E 本来就没在运行。"
rm -f "$HOME/Library/LaunchAgents/com.zhuangyi.remote-claude.plist"
read -k1 "?按任意键关闭窗口"
