#!/bin/zsh
# 让远窗开机自动运行、崩了自动重启。再次运行会用新代码重启它。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=local.anzhuo
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/anzhuo"
mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT/.venv/bin/anzhuo</string><string>run</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>zh_CN.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGDIR/out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/err.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && sleep 3 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "远窗已启动并设为开机自启。日志：$LOGDIR"
