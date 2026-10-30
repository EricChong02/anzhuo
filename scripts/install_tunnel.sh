#!/bin/zsh
# （可选，想在外面用手机访问时）把案头网页经 SSH 反向隧道接到你自己的服务器，由服务器上的 nginx 对外提供 HTTPS。
# 用法：scripts/install_tunnel.sh <~/.ssh/config 里的主机别名> [服务器端口=18765] [本机案头端口=8765]
# 开机自启、断了自动重连；重复运行即重启隧道。服务器那头怎么配见 docs/外网访问.md。
set -eu
if [[ $# -lt 1 ]]; then echo "用法：$0 <ssh 主机别名> [服务器端口=18765] [本机端口=8765]"; exit 1; fi
HOST=$1; PORT=${2:-18765}; LOCAL=${3:-8765}
DIR=${0:A:h}
LABEL=local.anzhuo.tunnel
PLIST=~/Library/LaunchAgents/$LABEL.plist
LOG=~/Library/Logs/anzhuo
mkdir -p "$LOG"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>$DIR/tunnel.sh</string><string>$HOST</string><string>$PORT</string><string>$LOCAL</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardErrorPath</key><string>$LOG/tunnel.log</string>
</dict></plist>
PLIST
launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null && sleep 2 || true
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "隧道已启动：本机 $LOCAL → $HOST 的 127.0.0.1:$PORT"
