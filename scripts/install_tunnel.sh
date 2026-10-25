#!/bin/zsh
# 把小E 网页（本机 127.0.0.1:8765）经 SSH 反向隧道接到阿里云 127.0.0.1:18765，由那边的 nginx 挂到 https://xiaoe.ericlab.cn。
# 开机自启、断了自动重连。用 ~/.ssh/config 里的 ericlab-aliyun（免密钥匙）。重复运行即重启隧道。
set -eu
LABEL=com.zhuangyi.remote-claude.tunnel
PLIST=~/Library/LaunchAgents/$LABEL.plist
LOG=~/Library/Logs/remote-claude
mkdir -p "$LOG"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/ssh</string><string>-N</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-R</string><string>127.0.0.1:18765:127.0.0.1:8765</string>
    <string>ericlab-aliyun</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardErrorPath</key><string>$LOG/tunnel.log</string>
</dict></plist>
PLIST
launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null && sleep 2 || true
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "隧道已启动：本机 8765 → 阿里云 127.0.0.1:18765"
