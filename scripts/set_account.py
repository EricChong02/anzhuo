"""设置远窗网页账号的密码：只把 PBKDF2 指纹存进钥匙串（remote-claude.web，账户名 = 账号名）。
账号本身（角色、工作文件夹、周额度比例）写在配置的 [accounts.名字] 里。
用法：.venv/bin/python scripts/set_account.py 账号名   （按提示输入密码；也可从标准输入给一行密码）"""
import getpass
import sys

from remote_claude.config import save_web_hash
from remote_claude.web import hash_password

if len(sys.argv) != 2:
    sys.exit("用法：set_account.py 账号名")
name = sys.argv[1].strip()
pw = getpass.getpass("密码：") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
if len(pw) < 6:
    sys.exit("密码至少 6 位")
save_web_hash(name, hash_password(pw))
print(f"已存 {name} 的密码指纹。小E 重启后生效。")
