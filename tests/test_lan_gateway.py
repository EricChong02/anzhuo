import importlib.util,socket,struct,asyncio
from pathlib import Path
import pytest
from aiohttp import web,ClientSession,DummyCookieJar
from aiohttp.test_utils import TestServer,TestClient
spec=importlib.util.spec_from_file_location('gw',Path(__file__).resolve().parents[1]/'scripts/lan_gateway.py');gw=importlib.util.module_from_spec(spec);spec.loader.exec_module(gw)
def query(name=gw.DOMAIN,typ=1):return struct.pack('!6H',42,256,1,0,0,0)+b''.join(bytes([len(p)])+p.encode() for p in name.split('.'))+b'\0'+struct.pack('!HH',typ,1)
@pytest.mark.parametrize('typ,count',[(1,1),(28,0),(65,0),(15,0)])
def test_local_dns(typ,count):
 r=gw.local_answer(query(typ=typ),'192.168.31.208');assert struct.unpack('!6H',r[:12])[3]==count
 if count:assert r.endswith(socket.inet_aton('192.168.31.208'))
def test_nonlocal():assert gw.local_answer(query('example.com'),'192.168.31.208') is None
@pytest.mark.parametrize('ip,result',[('192.168.31.2',True),('127.0.0.1',True),('10.0.0.1',True),('8.8.8.8',False),('::1',False),('invalid',False)])
def test_network(ip,result):assert gw.allowed(ip)==result
@pytest.mark.parametrize('data',[b'',bytes(12),query()[:16],struct.pack('!6H',42,256,1,0,0,0)+b'\xc0\x0c\0\1\0\1'])
def test_bad_dns(data):
 with pytest.raises((ValueError,IndexError)):gw.local_answer(data,'192.168.31.208')
def test_proxy_full_flow():
 async def run():
  async def upstream(req):
   if req.path=='/login' and req.method=='GET':return web.Response(text='<form></form>',headers={'Referrer-Policy':'no-referrer'})
   if req.path=='/login':
    form=await req.post();assert form['password']=='test-only'
    r=web.HTTPFound('/private');r.set_cookie('sid','test-session',secure=True,httponly=True,samesite='Lax');return r
   if req.path=='/stream':
    r=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await r.prepare(req);await r.write(b'data: first\n\n');await asyncio.sleep(.2);await r.write(b'data: last\n\n');return r
   return web.json_response({'cookie':req.cookies.get('sid'),'xff':req.headers.get('X-Forwarded-For')})
  app=web.Application();app.router.add_route('*','/{path:.*}',upstream)
  async with TestServer(app) as origin:
   old=gw.UPSTREAM;gw.UPSTREAM=str(origin.make_url('')).rstrip('/')
   async with ClientSession(cookie_jar=DummyCookieJar(),auto_decompress=False) as session:
    proxy=web.Application();proxy['client']=session;proxy.router.add_route('*','/{path:.*}',gw.proxy)
    async with TestClient(TestServer(proxy)) as client:
     headers={'Host':gw.DOMAIN}
     r=await client.get('/login',headers=headers);assert r.headers['Referrer-Policy']=='same-origin';await r.read()
     r=await client.post('/login',headers={**headers,'Origin':gw.ORIGIN},data={'password':'test-only'},allow_redirects=False)
     assert r.status==302 and r.headers['Location']=='/private';assert 'Secure' in r.headers['Set-Cookie']
     r=await client.get('/private',headers={**headers,'Cookie':'sid=test-session','X-Forwarded-For':'forged'});j=await r.json();assert j=={'cookie':'test-session','xff':'127.0.0.1'}
     r=await client.get('/private',headers=headers);assert (await r.json())['cookie'] is None
     for origin_value in ['null','https://evil.invalid']:
      r=await client.post('/login',headers={**headers,'Origin':origin_value});assert r.status==403
     r=await client.get('/private',headers={'Host':'evil.invalid'});assert r.status==421
     r=await client.get('/stream',headers=headers);assert await asyncio.wait_for(r.content.readuntil(b'\n\n'),.15)==b'data: first\n\n';assert b'last' in await r.read()
   gw.UPSTREAM=old
 asyncio.run(run())

def test_doh_and_profile():
 async def run():
  import base64,plistlib
  app=web.Application();app['lan_ip']='192.168.31.208';app.router.add_get('/dns-query',gw.doh);app.router.add_post('/dns-query',gw.doh)
  async with TestClient(TestServer(app)) as c:
   for typ,count in [(1,1),(28,0),(65,0)]:
    r=await c.post('/dns-query',data=query(typ=typ),headers={'Content-Type':'application/dns-message'})
    data=await r.read();assert r.status==200;assert struct.unpack('!6H',data[:12])[3]==count
    assert r.headers['Cache-Control']=='no-store'
   r=await c.get('/dns-query?dns='+base64.urlsafe_b64encode(query()).decode().rstrip('='));assert r.status==200;assert (await r.read()).endswith(socket.inet_aton('192.168.31.208'))
   r=await c.post('/dns-query',data=query('example.com'),headers={'Content-Type':'application/dns-message'});assert (await r.read())[3]&15==5
   r=await c.post('/dns-query',data=b'bad',headers={'Content-Type':'application/dns-message'});assert r.status==400
   r=await c.post('/dns-query',data=b'bad');assert r.status==415
  profile_spec=importlib.util.spec_from_file_location('profile_builder',Path(__file__).resolve().parents[1]/'scripts/make_profile.py');builder=importlib.util.module_from_spec(profile_spec);profile_spec.loader.exec_module(builder)
  p=plistlib.loads(plistlib.dumps(builder.build_profile()));assert len(p['PayloadContent'])==1
  settings=p['PayloadContent'][0];assert settings['DNSSettings']['AllowFailover'] is True
  assert settings['DNSSettings']['SupplementalMatchDomains']==[gw.DOMAIN]
  assert settings['OnDemandRules']==[{'Action':'Connect','InterfaceTypeMatch':'WiFi'},{'Action':'Disconnect'}]
 asyncio.run(run())
