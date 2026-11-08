"""正式部署自检：不登录、不发送消息、不修改手机或 Mac 的 DNS。"""
import asyncio,socket,struct
from aiohttp import ClientSession,TCPConnector,ClientTimeout
from aiohttp.abc import AbstractResolver
IP='192.168.31.208';DOMAIN='xiaoe.ericlab.cn'
def packet(name=DOMAIN,typ=1):
 return struct.pack('!6H',1234,256,1,0,0,0)+b''.join(bytes([len(s)])+s.encode() for s in name.split('.'))+b'\0'+struct.pack('!HH',typ,1)
class Local(AbstractResolver):
 async def resolve(self,host,port=0,family=socket.AF_INET):return [{'hostname':host,'host':IP,'port':port,'family':socket.AF_INET,'proto':0,'flags':0}]
 async def close(self):pass
async def main():
 async with ClientSession(connector=TCPConnector(resolver=Local()),timeout=ClientTimeout(total=10)) as c:
  for typ,count in [(1,1),(28,0),(65,0)]:
   async with c.post('https://'+DOMAIN+':8768/dns-query',data=packet(typ=typ),headers={'Content-Type':'application/dns-message'}) as r:
    data=await r.read();assert r.status==200;assert struct.unpack('!6H',data[:12])[3]==count
    if count:assert data.endswith(socket.inet_aton(IP))
  for path,status in [('/_x/health',200),('/_x/login',200),('/_x/api/sessions',401)]:
   async with c.get('https://'+DOMAIN+path,allow_redirects=False) as r:
    assert r.status==status,(path,r.status);await r.read()
  async with c.post('https://'+DOMAIN+'/_x/login',headers={'Origin':'https://evil.invalid'}) as r:assert r.status==403
 print('标准 HTTPS、公开证书、远窗专用加密 DNS、登录隔离和外站写入拦截通过')
asyncio.run(main())
