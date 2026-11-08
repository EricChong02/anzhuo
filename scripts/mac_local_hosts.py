"""仅将 Mac 自己的远窗域名指向本机；必须在网关自检通过后执行。"""
from pathlib import Path
import os,re,shutil
HOSTS=Path('/etc/hosts');BEGIN='# BEGIN remote-window local';END='# END remote-window local'
def update(text):
    text=re.sub(re.escape(BEGIN)+r'.*?'+re.escape(END)+r'\n?', '', text,flags=re.S)
    for line in text.splitlines():
        if 'xiaoe.ericlab.cn' in line.split('#',1)[0].split():raise RuntimeError('已有同域名 hosts 配置，保留现场并停止')
    return text.rstrip()+'\n\n'+BEGIN+'\n127.0.0.1 xiaoe.ericlab.cn\n'+END+'\n'
def main():
    if os.geteuid()!=0:raise SystemExit('需要系统管理员授权')
    original=HOSTS.read_text();new=update(original)
    if new==original:return
    backup=HOSTS.with_name('hosts.before-remote-window')
    if not backup.exists():shutil.copy2(HOSTS,backup)
    # 保留原文件的所有者、权限与系统扩展属性，不替换整个文件。
    HOSTS.write_text(new)
if __name__=='__main__':main()
