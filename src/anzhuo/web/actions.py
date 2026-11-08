"""对一个对话发话和操作：发送（带图、带文件）、改型号深度、“我在看”、撤回排队、确认、压缩、接力、停下。"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from pathlib import Path

from aiohttp import web

from .. import engines
from ..engines import EFFORTS, MODELS
from ..commands import Cmd
from ..dingtalk import Incoming
from ..folders import display

log = logging.getLogger(__name__)


class ActionsMixin:
    def _same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        return origin is None or origin == self.base or not self.base.startswith("https://")

    def _inbox(self, request: web.Request) -> Path:
        """网页传上来的附件放哪：admin 放远窗自己的收件箱；受限账号放进它自己的文件夹（不然它读不到）。"""
        root = self.root_of(request.get("acct", ""))
        return Path(root).expanduser() / "收到的附件" if root else self.store_path.parent / "inbox" / "web"

    async def _save_files(self, data: dict, folder: Path | None = None) -> list[str]:
        """网页传上来的文件（任何格式）存进收件箱，原文件名保留在后面。"""
        files = data.get("files") or []
        paths = []
        folder = folder or Path(self.store_path.parent) / "inbox" / "web"
        folder.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(files):
            name = Path(str(f.get("name") or "file")).name.replace("/", "_")[:120] or "file"
            p = folder / f"{int(time.time() * 1000)}-{i}-{name}"
            p.write_bytes(base64.b64decode(str(f.get("data", "")).partition(",")[2]))
            paths.append(str(p))
        return paths

    async def _save_images(self, data: dict, folder: Path | None = None) -> list[str]:
        paths = []
        folder = folder or Path(self.store_path.parent) / "inbox" / "web"
        folder.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate(data.get("images") or []):
            head, _, b64 = str(url).partition(",")
            ext = "jpg" if "jpeg" in head else "png"
            p = folder / f"{int(time.time() * 1000)}-{secrets.token_hex(3)}-{i}.{ext}"   # 一次反馈几处各带图：同一毫秒里也不撞名
            p.write_bytes(base64.b64decode(b64))
            paths.append(str(p))
        return paths

    async def _read_send(self, request: web.Request) -> tuple[dict, list[str]]:
        """普通附件流式落盘；保留旧网页的 JSON 上传兼容。"""
        if not request.content_type.startswith("multipart/"):
            return await request.json(), []
        reader = await request.multipart()
        data, saved = None, []
        folder = self._inbox(request)
        try:
            async for part in reader:
                if part.name == "message" and data is None and not saved:
                    raw = bytearray()
                    while chunk := await part.read_chunk(1024 * 1024):
                        raw.extend(chunk)
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("files"):
                        raise web.HTTPBadRequest(text="上传信息格式不正确")
                elif part.name == "files" and data is not None and part.filename:
                    folder.mkdir(parents=True, exist_ok=True)
                    name = Path(part.filename).name.replace("/", "_")[:120] or "file"
                    path = folder / f"{int(time.time() * 1000)}-{secrets.token_hex(6)}-{name}"
                    saved.append(str(path))
                    with path.open("wb") as f:
                        while chunk := await part.read_chunk(1024 * 1024):
                            f.write(chunk)
                else:
                    raise web.HTTPBadRequest(text="上传内容格式不正确")
            if data is None:
                raise web.HTTPBadRequest(text="缺少消息内容")
            return data, saved
        except BaseException as exc:
            for raw in saved:
                Path(raw).unlink(missing_ok=True)
            if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
                raise web.HTTPBadRequest(text="上传信息格式不正确") from None
            raise

    async def _api_send(self, request: web.Request) -> web.Response:
        """在某个对话里说话（它正在做就是插话）；没给 num 就是新开一个对话。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        data, streamed_files = await self._read_send(request)
        text = str(data.get("text") or "").strip()
        acct = request.get("acct", "")
        if self.role(acct) != "admin":
            return await self._send_fenced(request, acct, data, text, streamed_files)
        from ..schedules import parse_request
        try:
            plan = parse_request(text)
            if plan:
                if data.get("images") or data.get("files") or streamed_files:
                    raise ValueError("定时任务请先用文字创建；需要的文件放在工作目录中")
                from ..intent import inline_overrides
                m, e = inline_overrides(text)
                model = "gpt" if m == "gpt" else next((k for k, v in MODELS.items() if v == m), "auto")
                plan.update(target=data.get("num"), model=data.get("model") or model, effort=data.get("effort") or e or "medium",
                            folder=data.get("folder") if data.get("folder") in self.bridge.cfg.folders else "")
                async with self.bridge.schedules.lock:
                    task = self.bridge.jobs.create(plan)
                return web.json_response({"ok": True, "scheduled": task})
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        images = await self._save_images(data)
        uploaded = streamed_files + await self._save_files(data)
        if uploaded:
            text = (text + "\n\n" if text else "") + "\n".join(f"（附件已存到：{p}）" for p in uploaded)
        if not text and not images:
            return web.json_response({"ok": False, "error": "没写内容"}, status=400)
        b = self.bridge
        inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=text,
                       local_images=images, via="web")
        b.record("你（网页）", text, images=len(images), num=data.get("num"))
        num = data.get("num")
        if num:
            if hasattr(b, "registry") and (session := b.registry.get(int(num))):
                num = self._relay_latest(session).num
            busy = int(num) in b.runner.turns
            await b.message(Cmd("message", num=int(num), text=text), inc, routed=True)
            return web.json_response({"ok": True, "injected": busy})
        folder = str(data.get("folder") or "").strip()
        kind = None
        if pre := await self._ext_prepare(data, text):
            # 扩展接了（比如“接着做某门课”）：照它说的文件夹、开场白、档位开
            folder, kind = str(pre.get("folder") or ""), pre.get("kind")
            inc.text = text = str(pre.get("text") or text)
        elif folder == "auto":
            # 让远窗看话判断在哪做；判断不出或不涉及文件夹就用空白的临时文件夹
            from ..app import router   # 分派员只在私有版；没有时网页不给“自动”这一项，万一传来就当聊天
            r = await router.route(text, [], None, list(b.cfg.folders)) if router else None
            from .. import folders as fd
            folder = r.folder if r and r.folder and fd.candidates(r.folder, b.cfg) else ""
            kind = r.kind if r else None
        elif folder == "scratch":
            folder = ""
        elif folder and folder not in b.cfg.folders:
            # 手输的路径或文件夹名：网页上没法像钉钉那样回“选编号”，所以找不到、找到几个都当场告诉你
            from .. import folders as fd
            cands = fd.candidates(folder, b.cfg)
            if not cands:
                return web.json_response({"ok": False, "error": f"没找到文件夹“{folder}”。写完整路径（比如 ~/Projects/demo），或换一个文件夹名。"})
            if len(cands) > 1:
                return web.json_response({"ok": False, "error": f"“{folder}”找到好几个：" + "；".join(display(c) for c in cands[:4]) + "。请写完整路径。"})
            folder = str(cands[0])
        model, effort = self._model_effort(data)
        before = b.registry.next_num
        await b.new_session([folder] if folder else [], text, inc, note="（网页里新开）", kind=kind, model=model, effort=effort)
        return web.json_response({"ok": True, "num": before if b.registry.get(before) else None})

    async def _ext_prepare(self, data: dict, text: str) -> dict | None:
        for e in self.exts:
            try:
                if got := await e.prepare_new(data, text):
                    return got
            except Exception:  # noqa: BLE001
                log.exception("扩展 %s 新开前处理出错", e.name)
        return None

    async def _send_fenced(self, request: web.Request, acct: str, data: dict, text: str, streamed: list[str]) -> web.Response:
        """受限账号说话：只能在自己的对话里接着说，或在自己的文件夹里新开；不分派、不排定时、不用 GPT。"""
        b = self.bridge
        if why := self.over_quota(acct):
            return web.json_response({"ok": False, "error": why}, status=429)
        inbox = self._inbox(request)
        images = await self._save_images(data, inbox)
        uploaded = streamed + await self._save_files(data, inbox)
        if uploaded:
            text = (text + "\n\n" if text else "") + "\n".join(f"（附件已存到：{p}）" for p in uploaded)
        if not text and not images:
            return web.json_response({"ok": False, "error": "没写内容"}, status=400)
        inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=text,
                       local_images=images, via="web")
        owner = self.owner_key(acct)
        if num := data.get("num"):
            if not b.registry.mine(int(num), owner):
                raise web.HTTPNotFound()
            busy = int(num) in b.runner.turns
            await b.message(Cmd("message", num=int(num), text=text), inc, routed=True)
            return web.json_response({"ok": True, "injected": busy})
        root = Path(self.root_of(acct)).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        model, effort = self._model_effort(data)
        before = b.registry.reserve_num()
        b.record(f"你（网页 · {acct}）", text, images=len(images), num=before)
        await b.start_session(before, root, [], text, inc, kind="simple", model=None if engines.engine_of(model) else model, effort=effort,
                       owner=owner, root=str(root))
        return web.json_response({"ok": True, "num": before})

    @staticmethod
    def _model_effort(data: dict) -> tuple[str | None, str | None]:
        """网页上选的模型（opus / sonnet / haiku）和深度（low…max）；没选或不认得的返回 None。"""
        m = str(data.get("model") or "")
        return (m if engines.engine_of(m) else MODELS.get(m)), next((v for v in EFFORTS.values() if v and v == data.get("effort")), None)

    async def _api_settings(self, request: web.Request) -> web.Response:
        """改某个对话的模型和思考深度，下一轮起生效（正在做的这一轮不变）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        s = self.bridge.registry.get(int(request.match_info["num"]))
        if not s:
            raise web.HTTPNotFound()
        model, effort = self._model_effort(await request.json())
        if not engines.can_switch_model(s.engine, model):
            model = None   # 一个对话从头到尾用同一家：中途不在 Claude 和 GPT 之间换
        s.model = model or s.model
        s.effort = effort or s.effort
        self.bridge.registry.save()
        self._threads.pop(s.num, None)
        return web.json_response({"ok": True})

    async def _api_presence(self, request: web.Request) -> web.Response:
        """网页每 15 秒报一次“我正开着 #N 看”；切走、锁屏、5 分钟没碰时报不在。
        只认带 v=2 的：老页面不管人走没走都报“在看”（Mac 屏幕灭了 Safari 照样报），会把手机通知全挡掉。"""
        try:
            data = json.loads(await request.text() or "{}")
        except ValueError:
            data = {}
        num = int(data.get("num") or 0)
        if num and not self.bridge.registry.mine(num, request.get("owner", "admin")):
            return web.json_response({"ok": False}, status=404)
        if num and data.get("visible") and data.get("v") == 2:
            self.watch[num] = time.time()
        else:
            self.watch.pop(num, None) if num else self.watch.clear()
        return web.json_response({"ok": True})

    async def _api_unqueue(self, request: web.Request) -> web.Response:
        """撤回还排着没开始的最后一句（放回输入框）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        num = int(request.match_info["num"])
        q = b.runner.queued(num)
        if q and q[-1].parts and len(q[-1].parts) > 1:   # 并在一起的几句：只撤回最后补的那句
            job = q[-1]
            text = job.parts.pop()
            job.text = job.text[: -len(text) - 2] if job.text.endswith(text) else "\n\n".join(job.parts)
            out = b.outgoing.get(num, [])
            if out:
                out.pop()
            return web.json_response({"ok": True, "text": text.split("\n\n（附件已存到")[0]})
        job = b.runner.unqueue_last(num)
        if not job:
            return web.json_response({"ok": False, "error": "已经开始做了"})
        out = b.outgoing.get(num, [])
        if out:
            out.pop()
        return web.json_response({"ok": True, "text": job.text.split("\n\n（附件已存到")[0]})

    async def _api_confirm(self, request: web.Request) -> web.Response:
        """危险操作：你点了允许 / 不允许。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        allow = bool((await request.json()).get("allow"))
        ok = await self.bridge.answer_confirm(int(request.match_info["num"]), allow, "网页")
        return web.json_response({"ok": ok})

    async def _api_compact(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        msg = await self.bridge.compact(int(request.match_info["num"]), via="web")
        return web.json_response({"ok": True, "message": msg})

    async def _api_relay(self, request: web.Request) -> web.Response:
        """接力；可带 {to: 换到哪（sonnet / gpt …）, go: 开好就接着做}。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        num = int(request.match_info["num"])
        s = self.bridge.registry.get(num)
        if not s:
            raise web.HTTPNotFound()
        try:
            data = await request.json() if request.can_read_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        to, go = str((data or {}).get("to") or ""), bool((data or {}).get("go"))
        if why := self.bridge.relays.refuse(s, to):
            return web.json_response({"ok": False, "message": why}, status=409)
        msg = await self.bridge.relays.start(num, via="web", to=to, go=go)
        return web.json_response({"ok": True, "message": msg})

    async def _api_stop(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        stopped = await self.bridge.runner.stop(int(request.match_info["num"]))
        return web.json_response({"ok": True, "stopped": stopped})
