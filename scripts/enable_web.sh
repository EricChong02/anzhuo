#!/bin/zsh
# DNS 里加好 xiaoe.ericlab.cn → 47.107.105.209 之后跑一次：在阿里云签证书、加 HTTPS 段并重载 nginx，
# 再把本机配置里的 web_base 打开。小E 要重启才生效（在小E 的对话里改时用 restart_when_idle.py）。
set -eu
HOST_NAME=xiaoe.ericlab.cn
ssh ericlab-aliyun "set -e
getent ahostsv4 $HOST_NAME | grep -q 47.107.105.209 || { echo 'DNS 还没生效：$HOST_NAME 没解析到 47.107.105.209'; exit 1; }
[ -d /etc/letsencrypt/live/$HOST_NAME ] || certbot certonly --webroot -w /var/www/letsencrypt-xiaoe -d $HOST_NAME --non-interactive --agree-tos
grep -q 'listen 443' /etc/nginx/sites-available/$HOST_NAME || cat >> /etc/nginx/sites-available/$HOST_NAME <<'NGX'

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name xiaoe.ericlab.cn;
    ssl_certificate /etc/letsencrypt/live/xiaoe.ericlab.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/xiaoe.ericlab.cn/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    client_max_body_size 450m;
    add_header X-Robots-Tag \"noindex, nofollow\" always;
    location / {
        proxy_pass http://127.0.0.1:18765;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection \"\";
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
        proxy_buffering off;
        error_page 502 504 = @mac_off;
    }
    location @mac_off {
        default_type text/html;
        return 502 '<meta charset=utf-8><meta name=viewport content=\"width=device-width\"><p style=\"font:16px -apple-system;padding:24px\">Mac 那边没连上（可能睡着了或断网了）。稍后再试。</p>';
    }
}
NGX
nginx -t && systemctl reload nginx
curl -s -o /dev/null -w 'HTTPS 自测：%{http_code}（502 = Mac 那头网页还没开，重启小E 后就好）\n' --resolve $HOST_NAME:443:127.0.0.1 https://$HOST_NAME/_x/ || true"
sed -i '' 's|^# web_base = "https://xiaoe.ericlab.cn".*$|web_base = "https://xiaoe.ericlab.cn"  # 过程页与试课预览（阿里云 nginx + SSH 反向隧道）|' ~/.config/remote-claude/config.toml
grep -n '^web_base' ~/.config/remote-claude/config.toml
