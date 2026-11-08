"""用户在终端运行；仅安装独立网关，不重启远窗。"""
import getpass,json,os,pathlib,plistlib,shutil,subprocess,sys,shlex
ROOT=pathlib.Path(__file__).resolve().parent
HOME=pathlib.Path.home();DEST=HOME/'.local/share/remote-claude/lan'
LABEL='com.zhuangyi.remote-window.lan'
PYTHON=str(ROOT.parent/'.venv/bin/python')
if not pathlib.Path(PYTHON).exists():
    PYTHON='/Users/zhuangyi/Projects/04_项目/personal/remote-claude/.venv/bin/python'
def main():
    if os.geteuid()==0:raise SystemExit('请直接用普通用户运行安装脚本，不要在最前面加 sudo；系统安装步骤会单独请求密码。')
    if not pathlib.Path(PYTHON).exists():raise SystemExit('未找到远窗 Python 环境')
    installed=subprocess.run(['launchctl','print','system/'+LABEL],capture_output=True).returncode==0
    if installed:raise SystemExit('独立网关已经安装；如需更新请先按文档停止这个网关，不要重启远窗。')
    DEST.mkdir(parents=True,exist_ok=True);os.chmod(DEST,0o700)
    for name in ['lan_gateway.py','cert_agent.py']:
        shutil.copy2(ROOT/name,DEST/name)
    (DEST/'config.json').write_text(json.dumps({'ip':'192.168.31.208'}))
    sys.path.insert(0,str(DEST));import lan_gateway
    print('同步现有域名证书到登录钥匙串，私钥不写入安装包……',flush=True)
    value=lan_gateway.fetch_certificate();lan_gateway.keychain(value);del value
    log=HOME/'Library/Logs/remote-claude';log.mkdir(parents=True,exist_ok=True)
    plist={'Label':LABEL,'UserName':getpass.getuser(),'ProgramArguments':[PYTHON,str(DEST/'lan_gateway.py')],
           'WorkingDirectory':str(DEST),'EnvironmentVariables':{'HOME':str(HOME)},
           'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,
           'StandardOutPath':str(log/'lan.log'),'StandardErrorPath':str(log/'lan-error.log'),
           'Sockets':{name:{'SockFamily':'IPv4','SockType':typ,'SockServiceName':str(port),'SockNodeName':'0.0.0.0','SockPassive':True}
                      for name,typ,port in [('HTTPS','stream',443),('DOH','stream',8768)]}}
    staged=DEST/(LABEL+'.plist');staged.write_bytes(plistlib.dumps(plist))
    agent_label=LABEL+'-cert'
    agent=HOME/'Library/LaunchAgents'/f'{agent_label}.plist'
    agent.write_bytes(plistlib.dumps({'Label':agent_label,'ProgramArguments':[PYTHON,str(DEST/'cert_agent.py')],'WorkingDirectory':str(DEST),'EnvironmentVariables':{'HOME':str(HOME)},'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'StandardOutPath':str(log/'lan-cert.log'),'StandardErrorPath':str(log/'lan-cert-error.log')}))
    subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(agent)],check=True)
    target='/Library/LaunchDaemons/'+LABEL+'.plist'
    # sudo 只安装固定 plist 并登记独立服务；网关始终以普通用户运行。
    install_command=['/usr/bin/install','-o','root','-g','wheel','-m','644',str(staged),target]
    bootstrap_command=['/bin/launchctl','bootstrap','system',target]
    if '--gui' in sys.argv:
        script=shlex.join(install_command)+' && '+shlex.join(bootstrap_command)
        import json as _json
        subprocess.run(['/usr/bin/osascript','-e','do shell script '+_json.dumps(script,ensure_ascii=False)+' with administrator privileges'],check=True)
    else:
        subprocess.run(['sudo',*install_command],check=True)
        subprocess.run(['sudo',*bootstrap_command],check=True)
    print('独立网关安装完成。原远窗未重启。接着执行 verify.py 检查标准端口。')
if __name__=='__main__':main()
