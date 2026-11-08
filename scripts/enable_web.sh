#!/bin/zsh
# （可选）在你自己的服务器上签 HTTPS 证书、加一段 nginx 配置，把域名转到隧道端口。
# 用法：scripts/enable_web.sh <ssh 主机别名> <域名> [服务器端口=18765]
# 前提：域名的 DNS 已经解析到这台服务器；服务器是 Debian/Ubuntu 系，装好了 nginx 和 certbot，80 端口有现成的 server 段。
# 脚本只加一个站点，不碰别的站点；跑完后在配置里把 web_base 改成 https://你的域名，然后重启远窗。
set -eu
if [[ $# -lt 2 ]]; then echo "用法：$0 <ssh 主机别名> <域名> [服务器端口=18765]"; exit 1; fi
SSH_HOST=$1; HOST_NAME=$2; PORT=${3:-18765}
ssh "$SSH_HOST" "set -e
[ -d /etc/letsencrypt/live/$HOST_NAME ] || certbot certonly --webroot -w /var/www/letsencrypt-anzhuo -d $HOST_NAME --non-interactive --agree-tos
grep -q 'listen 443' /etc/nginx/sites-available/$HOST_NAME 2>/dev/null || cat >> /etc/nginx/sites-available/$HOST_NAME <<'NGX'

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name $HOST_NAME;
    ssl_certificate /etc/letsencrypt/live/$HOST_NAME/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$HOST_NAME/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    client_max_body_size 0;
    add_header X-Robots-Tag \"noindex, nofollow\" always;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection \"\";
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
        proxy_buffering off;
        error_page 502 504 = @offline;
    }
    location @offline {
        default_type text/html;
        return 502 '<meta charset=utf-8><meta name=viewport content=\"width=device-width\"><meta http-equiv=refresh content=15><p style=\"font:16px sans-serif;padding:24px\">电脑那边没连上（可能睡着了或断网了）。稍后再试。</p>';
    }
}
NGX
# enabled 可能是独立副本（不是软链接），不能只修改 available 后就认定已生效：两处都改，没有才建软链接
[ -e /etc/nginx/sites-enabled/$HOST_NAME ] || ln -s /etc/nginx/sites-available/$HOST_NAME /etc/nginx/sites-enabled/$HOST_NAME
for conf in /etc/nginx/sites-available/$HOST_NAME /etc/nginx/sites-enabled/$HOST_NAME; do
    if [ -f \"\$conf\" ]; then
        resolved=\$(readlink -f \"\$conf\")
        sed -i 's/client_max_body_size[[:space:]][^;]*;/client_max_body_size 0;/g' \"\$resolved\"
    fi
done
nginx -t && systemctl reload nginx
curl -s -o /dev/null -w 'HTTPS 自测：%{http_code}（502 = 电脑那头的远窗还没连上隧道，属正常）\n' --resolve $HOST_NAME:443:127.0.0.1 https://$HOST_NAME/_x/health || true"
echo "服务器这边好了。接着：在配置里写  web_base = \"https://$HOST_NAME\"，重启远窗，再运行 scripts/install_tunnel.sh $SSH_HOST $PORT（或在配置写 watch_host = \"$SSH_HOST\" 后不带参数）"
