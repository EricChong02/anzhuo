"""朗读回复：生成、播放、释放语音模型。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import quote

from aiohttp import web

from .common import COOKIE

log = logging.getLogger(__name__)


class AudioMixin:
    # ---------- 本地朗读 ----------
    async def _speech_context(self, app):
        task = asyncio.create_task(self.speech.maintenance())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.speech.close()

    @staticmethod
    def _speech_listener(request, tag):
        if not isinstance(tag, str) or len(tag) > 100:
            raise web.HTTPBadRequest()
        return hashlib.sha256((request.cookies.get(COOKIE, '') + ':' + tag).encode()).hexdigest()

    async def _release_speech(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        data = await request.json()
        await self.speech.release(self._speech_listener(request, data.get('listener', '')))
        return web.json_response({'ok': True})

    async def _api_speech(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        num = int(request.match_info["num"])
        session = self.bridge.registry.get(num)
        if not session or session.hidden:
            raise web.HTTPNotFound()
        data = await request.json()
        # 只朗读真实回复；客户端传轮次标识，不接受任意路径、命令或替换后的正文。
        text = ""
        if tid := data.get("tid"):
            if not isinstance(tid, str) or tid not in {p.stem for p in self._turn_paths(num)}:
                raise web.HTTPNotFound()
            text = (self._turn(tid) or {}).get("final_text", "")
        if not text and data.get("end"):
            items = await self._saved_items(session)
            text = next((it["text"] for it in items if it.get("who") == "claude" and it.get("end") == data["end"]), "")
        if not text:
            return web.json_response({"ok": False, "error": "这条回复还未保存，请稍后再点朗读"})
        try:
            return web.json_response({"ok": True, **await self.speech.start(num, text, listener=self._speech_listener(request, data.get('listener', '')))})
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)})

    def _speech_allowed(self, key: str, request: web.Request):
        try:
            m = self.speech.meta(key)
            session = self.bridge.registry.get(m["num"])
            if not session or session.hidden or session.owner != request.get("owner", "admin"):
                raise web.HTTPNotFound()
        except (ValueError, KeyError):
            raise web.HTTPNotFound() from None

    async def _api_audio(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        self._speech_allowed(key, request)
        if request.method == "POST":
            if not self._same_origin(request):
                raise web.HTTPForbidden()
            data = await request.json()
            self.speech.touch(key, playing=data.get("playing") is True)
            self.speech.renew(key, self._speech_listener(request, data.get('listener', '')))
        else:
            self.speech.renew(key, self._speech_listener(request, request.query.get('listener', '')))
        return web.json_response({"ok": True, **self.speech.status(key)}, headers={"Cache-Control": "no-store"})

    async def _audio(self, request: web.Request) -> web.StreamResponse:
        key = request.match_info["key"]
        self._speech_allowed(key, request)
        d = self.speech.directory(key)
        state = self.speech.status(key)
        if state["state"] == "error":
            return web.json_response({"error": state["error"]}, status=409)
        self.speech.touch(key, playing=request.query.get("download") != "1")
        headers = {"Content-Type": "audio/mpeg", "Cache-Control": "private, no-store",
                   "Content-Disposition": ("attachment" if request.query.get("download") == "1" else "inline") + "; filename*=UTF-8''" + quote("远窗朗读.mp3")}
        if state["state"] == "ready":
            return web.FileResponse(d / "audio.mp3", headers=headers)
        if request.query.get("download") == "1":
            return web.json_response({"error": "音频还在生成，请稍后下载"}, status=409)
        # 连续 HTTP 音频流：锁屏后无需依靠 JS 切换下一段。完成后的文件支持 Range 拖动。
        self.speech.readers[key] = self.speech.readers.get(key, 0) + 1
        resp = web.StreamResponse(headers={**headers, "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        if request.method == "HEAD":
            self.speech.readers[key] -= 1
            return resp
        offset = 0
        try:
            async with asyncio.timeout(1860):
                while True:
                    path = d / "audio.mp3" if (d / "audio.mp3").exists() else d / "audio.part"
                    if path.is_file():
                        with path.open("rb") as audio:
                            audio.seek(offset)
                            block = audio.read(64 * 1024)
                        if block:
                            await resp.write(block)
                            offset += len(block)
                            continue
                    state = self.speech.status(key)
                    if state["state"] in ("ready", "error"):
                        break
                    await asyncio.sleep(.1)
        except (ConnectionError, ValueError, OSError, TimeoutError):
            pass
        finally:
            self.speech.readers[key] = max(0, self.speech.readers.get(key, 1) - 1)
        return resp
