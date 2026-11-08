"""设置网页账号的密码：只把 PBKDF2 指纹存起来（macOS 存钥匙串 anzhuo.web，账户名 = 账号名；其它平台存 web_hash.json）。
账号本身（角色、工作文件夹、周额度比例）写在配置的 [accounts.名字] 里。
用法：.venv/bin/python scripts/set_account.py 账号名   （按提示输入密码；也可从标准输入给一行密码）"""
import getpass
import sys

from anzhuo.config import write_web_hash
from anzhuo.web import hash_password

if len(sys.argv) != 2:
    sys.exit("用法：.venv/bin/python scripts/set_account.py 账号名")
name = sys.argv[1].strip()
if not name:
    sys.exit("账号名不能为空")
pw = getpass.getpass("密码：") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
if len(pw) < 6:
    sys.exit("密码至少 6 位")
write_web_hash(name, hash_password(pw))
print(f"已存 {name} 的密码指纹。远窗重启后生效。")
