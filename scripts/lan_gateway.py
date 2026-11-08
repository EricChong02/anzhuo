"""远窗独立局域网网关。launchd 绑定标准端口，进程使用普通用户运行。"""
from __future__ import annotations
import asyncio, ctypes, ipaddress, json, os, socket, ssl, struct, subprocess, tempfile, time
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import web, ClientSession, ClientTimeout, DummyCookieJar
DOMAIN='xiaoe.ericlab.cn'
ORIGIN='https://'+DOMAIN
UPSTREAM='http://127.0.0.1:8765'
NETWORKS=[ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8')]
HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','content-length'}
def allowed(addr):
    try:return any(ipaddress.ip_address(addr) in n for n in NETWORKS)
    except ValueError:return False

def keychain(value=None):
    """通过系统 API 读写，不将私钥放入命令参数或日志。"""
    lib=ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    ptr=ctypes.c_void_p
    find=lib.SecKeychainFindGenericPassword
    find.argtypes=[ptr,ctypes.c_uint32,ptr,ctypes.c_uint32,ptr,ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ptr),ctypes.POINTER(ptr)]
    find.restype=ctypes.c_int32
    free=lib.SecKeychainItemFreeContent;free.argtypes=[ptr,ptr]
    release=cf.CFRelease;release.argtypes=[ptr]
    opened=ptr()
    open_keychain=lib.SecKeychainOpen;open_keychain.argtypes=[ctypes.c_char_p,ctypes.POINTER(ptr)];open_keychain.restype=ctypes.c_int32
    open_status=open_keychain(str(Path.home()/'Library/Keychains/login.keychain-db').encode(),ctypes.byref(opened))
    if open_status:raise RuntimeError(f'无法打开登录钥匙串 OSStatus={open_status}')
    service=b'remote-claude.lan-tls';account=DOMAIN.encode()
    size=ctypes.c_uint32();data=ptr();item=ptr()
    status=find(opened,len(service),service,len(account),account,ctypes.byref(size),ctypes.byref(data),ctypes.byref(item))
    try:
        if value is None:
            if status:raise RuntimeError(f'钥匙串证书暂不可读 OSStatus={status}')
            return json.loads(ctypes.string_at(data,size.value))
        payload=json.dumps(value).encode()
        if status==0:
            modify=lib.SecKeychainItemModifyAttributesAndData;modify.argtypes=[ptr,ptr,ctypes.c_uint32,ptr];modify.restype=ctypes.c_int32
            status=modify(item,None,len(payload),payload)
        elif status==-25300:
            add=lib.SecKeychainAddGenericPassword;add.argtypes=[ptr,ctypes.c_uint32,ptr,ctypes.c_uint32,ptr,ctypes.c_uint32,ptr,ptr];add.restype=ctypes.c_int32
            status=add(opened,len(service),service,len(account),account,len(payload),payload,None)
        if status:raise RuntimeError(f'钥匙串证书保存失败 OSStatus={status}')
    finally:
        if data:free(None,data)
        if item:release(item)
        if opened:release(opened)

def fetch_certificate():
    # ssh 主机密钥必须已信任；输出只进入内存，不输出私钥。
    command="python3 -c 'import pathlib,json; p=pathlib.Path(\"/etc/letsencrypt/live/xiaoe.ericlab.cn\"); print(json.dumps({k:(p/v).read_text() for k,v in [(\"cert\",\"fullchain.pem\"),(\"key\",\"privkey.pem\")]}))'"
    r=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','ericlab-aliyun',command],capture_output=True,timeout=20)
    if r.returncode:raise RuntimeError('证书同步失败')
    value=json.loads(r.stdout)
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert=x509.load_pem_x509_certificate(value['cert'].encode())
    sans=cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    if DOMAIN not in sans or cert.not_valid_after_utc.timestamp()<time.time()+86400:raise RuntimeError('证书域名或有效期不符合要求')
    key=serialization.load_pem_private_key(value['key'].encode(),None)
    if key.public_key().public_numbers()!=cert.public_key().public_numbers():raise RuntimeError('证书密钥不匹配')
    return value

def load_tls(context,value):
    # OpenSSL 接口需要文件；仅在权限 0700 临时目录短暂使用，加载后立即删除。
    with tempfile.TemporaryDirectory(prefix='remote-window-tls-') as d:
        cert=Path(d)/'cert.pem';key=Path(d)/'key.pem'
        for p,field in [(cert,'cert'),(key,'key')]:
            fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(value[field])
        context.load_cert_chain(cert,key)

def activated(name):
    lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib');func=lib.launch_activate_socket
    func.argtypes=[ctypes.c_char_p,ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),ctypes.POINTER(ctypes.c_size_t)]
    fds=ctypes.POINTER(ctypes.c_int)();count=ctypes.c_size_t()
    if func(name.encode(),ctypes.byref(fds),ctypes.byref(count)):raise RuntimeError('未收到 launchd 监听套接字')
    result=[socket.socket(fileno=fds[i]) for i in range(count.value)]
    lib.free.argtypes=[ctypes.c_void_p];lib.free(fds)
    for s in result:s.setblocking(False)
    return result

async def proxy(req):
    if not allowed(req.remote):raise web.HTTPForbidden()
    if req.host.lower()!=DOMAIN:raise web.HTTPMisdirectedRequest()
    origin=req.headers.get('Origin')
    if req.method not in {'GET','HEAD','OPTIONS'} and origin and origin!=ORIGIN:raise web.HTTPForbidden()
    headers={k:v for k,v in req.headers.items() if k.lower() not in HOP|{'host','forwarded','x-forwarded-for','x-forwarded-proto'}}
    headers['Host']=DOMAIN;headers['X-Forwarded-For']=req.remote;headers['X-Forwarded-Proto']='https'
    async with req.app['client'].request(req.method,UPSTREAM+req.raw_path,headers=headers,data=req.content.iter_any(),allow_redirects=False) as upstream:
        resp=web.StreamResponse(status=upstream.status)
        for k,v in upstream.headers.items():
            if k.lower() not in HOP:
                if k.lower()=='referrer-policy':v='same-origin'
                resp.headers.add(k,v)
        await resp.prepare(req)
        try:
            async for chunk in upstream.content.iter_any():await resp.write(chunk)
            await resp.write_eof()
        except (ConnectionResetError,asyncio.CancelledError):pass
        return resp


def question(data):
    if len(data)<17:raise ValueError('DNS 包太短')
    ident,flags,qd,*_=struct.unpack('!6H',data[:12])
    if qd!=1 or flags&0xf800:raise ValueError('仅处理单个普通 DNS 问题')
    labels=[];i=12
    while True:
        n=data[i];i+=1
        if not n:break
        if n>63 or i+n>len(data):raise ValueError('非法 DNS 名称')
        labels.append(data[i:i+n].decode('ascii').lower());i+=n
        if i>267:raise ValueError('DNS 名称过长')
    if i+4>len(data):raise ValueError('DNS 问题不完整')
    typ,cls=struct.unpack('!HH',data[i:i+4]);return ident,flags,'.'.join(labels),typ,cls,i+4

def local_answer(data,ip):
    ident,flags,name,typ,cls,end=question(data)
    if name!=DOMAIN:return None
    # AAAA 和 HTTPS/SVCB 留空，防止浏览器改走公网 IPv6 或其它端口。
    answer=b''
    if typ==1 and cls==1:answer=b'\xc0\x0c'+struct.pack('!HHIH',1,1,5,4)+socket.inet_aton(ip)
    return struct.pack('!6H',ident,0x8080|(flags&0x0100),1,int(bool(answer)),0,0)+data[12:end]+answer

async def doh(req):
    if not allowed(req.remote):raise web.HTTPForbidden()
    if req.method=='GET':
        import base64
        raw=req.query.get('dns','')
        if len(raw)>8192:raise web.HTTPRequestEntityTooLarge(max_size=8192,actual_size=len(raw))
        try:data=base64.urlsafe_b64decode(raw+'='*((-len(raw))%4))
        except Exception:raise web.HTTPBadRequest()
    else:
        if req.content_type!='application/dns-message':raise web.HTTPUnsupportedMediaType()
        data=await req.read()
    if len(data)>4096:raise web.HTTPRequestEntityTooLarge(max_size=4096,actual_size=len(data))
    try:
        answer=local_answer(data,req.app['lan_ip'])
        # 此入口只解析远窗；其它网站继续使用手机原来的 DNS。
        if answer is None:
            ident,flags,name,typ,cls,end=question(data)
            answer=struct.pack('!6H',ident,0x8085|(flags&0x0100),1,0,0,0)+data[12:end]
    except (ValueError,IndexError,UnicodeError):raise web.HTTPBadRequest()
    return web.Response(body=answer,content_type='application/dns-message',headers={'Cache-Control':'no-store'})

async def certificate_from_agent():
    path=Path.home()/'.local/share/remote-claude/lan/certificate.sock'
    reader,writer=await asyncio.wait_for(asyncio.open_unix_connection(str(path)),5)
    try:
        writer.write(b'1');await writer.drain()
        size=struct.unpack('!I',await asyncio.wait_for(reader.readexactly(4),10))[0]
        if size>65536:raise RuntimeError('证书消息过长')
        return json.loads(await asyncio.wait_for(reader.readexactly(size),10))
    finally:
        writer.close();await writer.wait_closed()

async def renew(context):
    while True:
        await asyncio.sleep(300)
        try:
            value=await certificate_from_agent();load_tls(context,value)
        except Exception:print('证书刷新暂未成功，保留当前证书',flush=True)

async def main():
    cfg=json.loads((Path(__file__).parent/'config.json').read_text())
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    value=await certificate_from_agent()
    load_tls(context,value)
    async with ClientSession(timeout=ClientTimeout(total=None,sock_connect=10),auto_decompress=False,cookie_jar=DummyCookieJar()) as client:
        app=web.Application(client_max_size=100*1024**2);app['client']=client;app['lan_ip']=cfg['ip']
        app.router.add_get('/dns-query',doh);app.router.add_post('/dns-query',doh)
        app.router.add_route('*','/{path:.*}',proxy)
        runner=web.AppRunner(app,access_log=None);await runner.setup()
        for name in ['HTTPS','DOH']:
            for s in activated(name):await web.SockSite(runner,s,ssl_context=context).start()
        task=asyncio.create_task(renew(context));print('局域网 HTTPS 与远窗专用加密 DNS 已启动',flush=True)
        try:await asyncio.Event().wait()
        finally:
            task.cancel();await runner.cleanup()

if __name__=='__main__':asyncio.run(main())
