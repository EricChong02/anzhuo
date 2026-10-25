"""设置小E 网页的登录账号密码：密码只把 PBKDF2 指纹存进钥匙串（remote-claude.web），账号写进配置的 web_user。
用法：.venv/bin/python scripts/set_web_password.py   （按提示输入；也可从标准输入给两行：账号、密码）"""
import getpass
import subprocess
import sys

from remote_claude.config import CONFIG_PATH, WEB_KEYCHAIN
from remote_claude.web import hash_password

if sys.stdin.isatty():
    user = input("账号：").strip()
    pw = getpass.getpass("密码：")
else:
    user, pw = (sys.stdin.readline().strip() for _ in range(2))
if not user or not pw:
    sys.exit("账号和密码都不能空")
subprocess.run(["security", "add-generic-password", "-U", "-s", WEB_KEYCHAIN, "-a", user, "-w", hash_password(pw)],
               check=True)
text = CONFIG_PATH.read_text()
lines = [ln for ln in text.splitlines() if not ln.startswith("web_user")]
i = next((k for k, ln in enumerate(lines) if ln.startswith("web_base")), len(lines))
lines.insert(i + 1, f'web_user = "{user}"  # 网页登录账号；密码指纹在钥匙串 {WEB_KEYCHAIN}')
CONFIG_PATH.write_text("\n".join(lines) + "\n")
print("已设置：账号写进配置，密码指纹存进钥匙串。小E 重启后生效。")
