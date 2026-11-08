#!/bin/zsh
# 把钉钉应用的 Client Secret 存进钥匙串。密钥只在这个窗口里输入，不显示、不进任何文件或命令记录。
set -e
CONFIG="${ANZHUO_CONFIG:-$HOME/.config/anzhuo/config.toml}"
CLIENT_ID=$(sed -n 's/^client_id *= *"\(.*\)"/\1/p' "$CONFIG")
if [[ -z "$CLIENT_ID" ]]; then echo "配置 $CONFIG 里没有 client_id"; exit 1; fi
echo "应用 Client ID：$CLIENT_ID"
echo "下面会问两次 password data：都粘贴钉钉开发者后台“凭证与基础信息”里的 Client Secret（看不到输入是正常的），各按一次回车。"
security add-generic-password -U -s anzhuo.dingtalk -a "$CLIENT_ID" -w
echo "已存好。"
