#!/bin/zsh
# 隧道本体，由 install_tunnel.sh 装的 launchd 调用，不要手动跑。
# 参数：ssh 别名  服务器上的端口  本机远窗网页端口
# 网络断过一下时，服务器那边常还留着上一条隧道的 sshd，占着端口，新隧道连不上、网页一直转圈。
# launchd 只在上一条隧道退出后才再跑这里，所以此时占着端口的 sshd 一定是旧的，先清掉再连。
HOST=$1
PORT=${2:-18765}
LOCAL=${3:-8765}
stale=$(/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=15 $HOST \
  "pids=\$(ss -tlnpH 'sport = :$PORT' | grep -o '\"sshd\",pid=[0-9]*' | grep -o '[0-9]*\$'); [ -n \"\$pids\" ] && kill \$pids && echo \$pids" 2>/dev/null)
if [[ -n $stale ]]; then
  echo "$(date '+%F %T') 清掉服务器上占着 $PORT 的旧隧道：$stale" >&2
  sleep 1
fi
exec /usr/bin/ssh -N \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:$PORT:127.0.0.1:$LOCAL \
  $HOST
