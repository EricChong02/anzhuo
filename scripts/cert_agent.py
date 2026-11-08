"""登录会话中的证书助手。只通过用户私有 Unix socket 向独立网关提供证书。"""
import asyncio,json,os,socket,struct,time
from pathlib import Path
from lan_gateway import keychain,fetch_certificate
ROOT=Path.home()/'.local/share/remote-claude/lan'
async def reply(reader,writer):
    try:
        if await asyncio.wait_for(reader.readexactly(1),5)!=b'1':return
        value=await asyncio.to_thread(keychain)
        payload=json.dumps(value).encode();writer.write(struct.pack('!I',len(payload))+payload);await writer.drain()
    except Exception:pass
    finally:
        writer.close()
        try:await writer.wait_closed()
        except OSError:pass
async def renew():
    while True:
        delay=6*3600
        try:
            value=await asyncio.to_thread(fetch_certificate);await asyncio.to_thread(keychain,value)
            print('公开证书同步成功',flush=True)
        except Exception:
            delay=300;print('公开证书同步暂未成功，保留已有证书，5分钟后重试',flush=True)
        await asyncio.sleep(delay)
async def main():
    ROOT.mkdir(parents=True,exist_ok=True);os.chmod(ROOT,0o700)
    path=ROOT/'certificate.sock'
    try:path.unlink()
    except FileNotFoundError:pass
    server=await asyncio.start_unix_server(reply,str(path));os.chmod(path,0o600)
    task=asyncio.create_task(renew())
    print('登录会话证书助手已启动',flush=True)
    try:
        async with server:await server.serve_forever()
    finally:
        task.cancel()
        try:path.unlink()
        except FileNotFoundError:pass
if __name__=='__main__':asyncio.run(main())
