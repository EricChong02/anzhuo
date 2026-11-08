#!/bin/zsh
# （想在外面用手机访问时）把网页经 SSH 反向隧道接到你自己的服务器，由服务器上的 nginx 对外提供 HTTPS。
# 用法：scripts/install_tunnel.sh [~/.ssh/config 里的主机别名=配置里的 watch_host] [服务器端口=18765] [本机端口=8765]
# 开机自启、断了自动重连；每次连接前先清掉服务器上残留的旧隧道（见 tunnel.sh）。重复运行即重启隧道。
set -eu
CONFIG="${ANZHUO_CONFIG:-$HOME/.config/anzhuo/config.toml}"
HOST=${1:-$(sed -n 's/^watch_host *= *"\([^"]*\)".*/\1/p' "$CONFIG" 2>/dev/null)}
PORT=${2:-18765}; LOCAL=${3:-8765}
if [[ -z $HOST ]]; then echo "用法：$0 <ssh 主机别名> [服务器端口=18765] [本机端口=8765]（或在配置里写 watch_host）"; exit 1; fi
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
