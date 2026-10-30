#!/bin/zsh
# 双击：立刻停掉案头，并取消开机自启。要恢复就运行 scripts/install_launchd.sh。
launchctl bootout "gui/$(id -u)/local.anzhuo" 2>/dev/null && echo "案头已停用。" || echo "案头本来就没在运行。"
rm -f "$HOME/Library/LaunchAgents/local.anzhuo.plist"
read -k1 "?按任意键关闭窗口"
