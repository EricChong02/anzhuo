"""手机快捷指令上传：iOS 分享菜单里选中的文件/照片，带上附言，直接交给 Claude。

入口是 {px}/q/<令牌>，不走登录（令牌就是凭据，只在局域网网关后面可达）。
一次分享多个文件时，快捷指令逐个 POST 并带同一个 batch，这里等 2 秒没有新文件再合成一条消息，
免得十张照片开十个对话。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

from ..dingtalk import Incoming

log = logging.getLogger(__name__)

TOKEN_FILE = "shortcut.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp"}
QUIET = 2.0   # 最后一个文件到齐后再等这么久就合并提交


class QuickMixin:
    def _shortcut_token(self) -> str:
        """令牌第一次用时生成，存在数据目录（不进配置、不进 git）。"""
        p = Path(self.store_path).parent / TOKEN_FILE
        try:
            token = str(json.loads(p.read_text()).get("token") or "")
            if token:
                return token
        except (OSError, ValueError):
            pass
        token = secrets.token_urlsafe(24)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"token": token}))
            p.chmod(0o600)
        except OSError:
            log.exception("快捷指令令牌存不下来")
        return token

    def _quick_ok(self, token: str) -> bool:
        return secrets.compare_digest(token, self._shortcut_token())

    async def _api_quick(self, request: web.Request) -> web.Response:
        """快捷指令传一个文件（multipart 的 file 字段，或直接 POST 文件体）。"""
        if not self._quick_ok(request.match_info["token"]):
            raise web.HTTPNotFound()
        # 小E 的网页把上限设成 0（本意“不限”）；aiohttp 的 multipart 解析器却把它当成“最多 0 字节”，这里放开
        request._client_max_size = sys.maxsize
        query = request.query
        batch = query.get("batch", "").strip()[:40]
        text = query.get("text", "")[:4000]   # aiohttp 已经把地址里的中文解好了
        folder = Path(self.store_path).parent / "inbox" / "web"
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        try:
            if request.content_type.startswith("multipart/"):
                reader = await request.multipart()
                async for part in reader:
                    if part.filename is None:
                        if part.name in ("text", "message") and not text:
                            text = (await part.text())[:4000]
                        continue
                    saved.append(await self._quick_save(part, folder))
            else:
                name = query.get("name", "") or unquote(request.headers.get("X-Filename", ""))
                if not name:   # 快捷指令没给文件名时按类型补个扩展名，图片才认得出
                    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
                           "image/webp": ".webp", "application/pdf": ".pdf"}.get(request.content_type, "")
                    name = "file" + ext
                safe = Path(name).name.replace("/", "_")[:120] or "file"
                p = folder / f"{int(time.time() * 1000)}-{secrets.token_hex(6)}-{safe}"
                size = 0
                with p.open("wb") as f:   # 流式落盘：网页的 client_max_size 是 0，read() 会被当成“最大 0 字节”
                    while chunk := await request.content.read(1024 * 1024):
                        f.write(chunk)
                        size += len(chunk)
                if size:
                    saved.append(str(p))
                else:
                    p.unlink(missing_ok=True)
        except BaseException:
            for raw in saved:
                Path(raw).unlink(missing_ok=True)
            raise
        if not saved and not text:
            return web.json_response({"ok": False, "error": "没收到内容"}, status=400)
        if batch:
            self._quick_queue(batch, text, saved)
            return web.json_response({"ok": True, "queued": True})
        num = await self._quick_start(text, saved)
        return web.json_response({"ok": True, "num": num})

    async def _quick_save(self, part, folder: Path) -> str:
        name = Path(unquote(part.filename)).name.replace("/", "_")[:120] or "file"   # 中文名可能带着百分号编码
        p = folder / f"{int(time.time() * 1000)}-{secrets.token_hex(6)}-{name}"
        with p.open("wb") as f:
            while chunk := await part.read_chunk(1024 * 1024):
                f.write(chunk)
        return str(p)

    def _quick_queue(self, batch: str, text: str, saved: list[str]) -> None:
        """同一个 batch 的文件先攒着，2 秒没有新的再一起交出去。"""
        q = self._quick_pending.setdefault(batch, {"text": "", "files": [], "handle": None})
        if text and not q["text"]:
            q["text"] = text
        q["files"].extend(saved)
        if q["handle"]:
            q["handle"].cancel()
        q["handle"] = asyncio.get_running_loop().call_later(
            QUIET, lambda: asyncio.get_running_loop().create_task(self._quick_flush(batch)))

    async def _quick_flush(self, batch: str) -> None:
        q = self._quick_pending.pop(batch, None)
        if not q:
            return
        try:
            await self._quick_start(q["text"], q["files"])
        except Exception:  # noqa: BLE001
            log.exception("快捷指令上传合并后发送失败")

    async def _quick_start(self, text: str, saved: list[str]) -> int | None:
        """和小E 收到钉钉文件一样：附件进收件箱，附言原样，新开一个对话去做。"""
        b = self.bridge
        if b is None:
            raise web.HTTPInternalServerError(text="小E 还没起来")
        if saved:
            note = "\n".join(f"（附件已存到：{p}）" for p in saved)
            text = (text + "\n\n" if text else "") + note
        images = [p for p in saved if Path(p).suffix.lower() in IMAGE_EXTS]
        inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=text,
                       local_images=images, via="web")
        before = b.registry.next_num
        b.record("你（手机分享）", text, images=len(images), num=before)
        await b.new_session([], text, inc, note="（手机上分享过来）")
        return before if b.registry.get(before) else None
