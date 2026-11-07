"""设置 accounts 中某个账号的密码，macOS 存钥匙串，其它平台存密码指纹文件。"""
import getpass
import sys
from anzhuo.config import write_web_hash
from anzhuo.web import hash_password

if len(sys.argv) != 2:
    sys.exit("用法：.venv/bin/python scripts/set_account.py 账号名")
name = sys.argv[1].strip()
if not name:
    sys.exit("账号名不能为空")
password = getpass.getpass("密码：")
if len(password) < 6:
    sys.exit("密码至少 6 位")
write_web_hash(name, hash_password(password))
print(f"已存 {name} 的密码指纹，重启后生效。")
