"""远窗网页：对话框、完整过程页、通知、文件收发。只听本机端口，要从外面访问就自己放一层反向代理或隧道（见 docs/外网访问.md）。

进门：账号密码，登录一次这台设备 30 天免登录（Cookie）。密码只存 PBKDF2 指纹，
同一来源连错 5 次锁 15 分钟、所有来源一小时内错 30 次全体锁一小时。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import shutil
import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from . import thread, translate
from .brand import LOGIN_CSS, MARK
from .push import Push
from .speech import Speech
from .sessions import COMPACT, Session
from .cards import parse_decision
from .sessions import NOISE
from .commands import Cmd
from .dingtalk import Incoming
from .folders import display
from .report import EFFORT_CN, LIMIT_CN, model_name

log = logging.getLogger(__name__)

COOKIE = "anzhuo_sid"
SESSION_DAYS = 30
LOCK_IP = (5, 15 * 60)  # 同一来源：错几次、锁多久
LOCK_ALL = (30, 60 * 60)  # 所有来源合计：一小时内错几次就全体锁一小时


def hash_password(password: str, salt: str | None = None, iters: int = 300_000) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iters).hex()
    return f"pbkdf2${iters}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        return hmac.compare_digest(hash_password(password, salt, int(iters)).split("$")[3], digest)
    except ValueError:
        return False

DESK_FRESH = 300  # 电脑端对话的记录在这么多秒内被写过，就当电脑上可能还开着
PREFIX = "/_x"  # 远窗自己的页面都在这个前缀下面
def display_model(model_id: str) -> str:
    """统一网页名称，保留未知型号；兼容早先保存的 GPT（实际型号）。"""
    wrapped = re.fullmatch(r"GPT[（(](.*)[）)]", model_id or "")
    mid = wrapped[1] if wrapped else model_id or ""
    if mid in ("Codex", "GPT", "gpt"):
        return "GPT"  # 没有型号证据时不编版本；网页上两家统一叫 Claude / GPT（Codex 是干活的工具，不是模型）
    if mid.startswith("claude-"):
        label = model_name(mid)
        return "Claude " + label if label != mid else mid
    if mid.lower().startswith("gpt-"):
        return "GPT-" + mid[4:].replace("-", " ").title()
    return mid


def model_options() -> list[list[str]]:
    from .app import MODELS
    from .codex import configured_model
    return [[key, display_model(MODELS[key])] for key in ("opus", "sonnet", "haiku", "fable")] + [
        ["gpt", display_model(configured_model()) or "GPT"]]

def _md(text: str) -> str:
    """回复里的 Markdown 粗略转成 HTML：转义后只处理加粗、行内代码和链接，其余保留换行。"""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{title}</title>
<meta name="theme-color" content="#d5dee5" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#2e3a45" media="(prefers-color-scheme: dark)">
<style>
:root{{--sky:linear-gradient(158deg,#d4e0ea 0%,#e1eaf1 38%,#ecf2f6 72%,#f3f6f9 100%);--ink:#25313e;--mute:#566474;--faint:#8592a0;--line:rgba(24,44,64,.065);
  --acc:#2c4a74;--acc-hi:#4b78b4;--acc-glow:rgba(66,108,170,.55);--accsoft:rgba(44,74,116,.08);--red:#b0443a;
  --glass:rgba(255,255,255,.60);--glass-hi:rgba(255,255,255,.72);--edge:rgba(255,255,255,.92);--hi:rgba(255,255,255,.95);--glow:rgba(58,98,156,.40);
  --shadow:10px 18px 48px -12px rgba(92,124,168,.22),3px 6px 14px -6px rgba(92,124,168,.10);--blur:saturate(1.15) blur(24px);
  --ff:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;--fd:-apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC",sans-serif}}
@media (prefers-color-scheme:dark){{:root{{--sky:linear-gradient(176deg,#151b22 0%,#11171d 50%,#0d1217 100%);--ink:#e6ebf0;--mute:#a3aeb9;--faint:#76828d;--line:rgba(255,255,255,.075);
  --acc:#9dbbe4;--acc-hi:#8db4ee;--acc-glow:rgba(122,166,234,.55);--accsoft:rgba(141,180,238,.11);--red:#ee8b7c;
  --glass:rgba(255,255,255,.045);--glass-hi:rgba(255,255,255,.08);--edge:rgba(255,255,255,.085);--hi:rgba(255,255,255,.10);--glow:rgba(96,140,210,.22);
  --shadow:10px 18px 48px -12px rgba(0,0,0,.5),3px 6px 14px -6px rgba(0,0,0,.3)}}}}
html{{background:var(--sky) fixed;min-height:100%}}
body{{margin:0;color:var(--ink);font:400 16px/1.75 var(--ff);letter-spacing:.01em;-webkit-font-smoothing:antialiased}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 48px}}
h1{{font:300 30px/1.3 var(--fd);letter-spacing:.06em;margin:18px 0 6px}} h2{{font-size:11.5px;font-weight:500;letter-spacing:.14em;margin:28px 0 8px;color:var(--faint)}}
b,strong,th{{font-weight:500}} .meta{{color:var(--mute);font-size:14px}} a{{color:var(--acc)}}
.card{{background:var(--glass);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);border-radius:16px;padding:14px 16px;margin:10px 0;
  box-shadow:inset 0 0 0 1px var(--edge)}}
.say{{white-space:pre-wrap}} code{{font:400 13px ui-monospace,"SF Mono",Menlo,monospace;background:var(--glass);box-shadow:inset 0 0 0 1px var(--edge);padding:1px 6px;border-radius:6px}}
pre{{white-space:pre-wrap;word-break:break-all;font:400 13px/1.55 ui-monospace,"SF Mono",Menlo,monospace;margin:6px 0 0}}
.step{{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--line)}} .step:last-child{{border:0}}
.t{{color:var(--faint);font:400 12px ui-monospace,"SF Mono",Menlo,monospace;min-width:58px;padding-top:4px;font-variant-numeric:tabular-nums}}
details summary{{cursor:pointer;color:var(--mute)}} img{{max-width:100%;border-radius:12px;box-shadow:inset 0 0 0 1px var(--edge);margin:6px 0}}
.btn{{display:inline-block;color:var(--acc);text-decoration:none;padding:9px 18px;border-radius:999px;margin:4px 6px 4px 0;font-weight:500;background:var(--accsoft);box-shadow:inset 0 0 0 1px rgba(44,74,116,.14)}}
ul{{padding-left:20px;margin:4px 0}} li::marker{{color:var(--faint)}}
.card > a{{text-decoration:none;font-size:17px;color:var(--ink)}} details li{{margin:4px 0}} details li a{{color:var(--ink);text-decoration:none;font-size:15px}}
.bk{{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:4px;padding:calc(10px + env(safe-area-inset-top)) 14px 10px;
  background:rgba(236,241,244,.5);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);border-bottom:1px solid var(--line);color:var(--acc);font-size:16px;text-decoration:none}}
@media (prefers-color-scheme:dark){{.bk{{background:rgba(14,20,27,.6)}}}}
.mk{{display:block;overflow:hidden}} .mk>svg{{display:block;width:100%;height:100%}} .mk>svg.d{{display:none}}
@media (prefers-color-scheme:dark){{.mk>svg.l{{display:none}} .mk>svg.d{{display:block}}}}
::selection{{background:rgba(75,120,180,.22)}}
/* 第五版：扁平磨砂（对照参考图） */
:root{{--sky:linear-gradient(200deg,#d5dee5 0%,#e3e9ed 34%,#edf1f3 66%,#f5f7f7 100%);--ink:#34434f;--mute:#67757f;--faint:#909ca6;
  --glass:rgba(255,255,255,.52);--glass-hi:rgba(255,255,255,.66);--knob-in:rgba(60,84,104,.07);--shadow:none}}
@media (prefers-color-scheme:dark){{:root{{--sky:linear-gradient(200deg,#2e3a45 0%,#222c35 40%,#182027 100%);--ink:#e2e8ed;--mute:#9eabb6;--faint:#6f7c87;
  --glass:rgba(255,255,255,.055);--glass-hi:rgba(255,255,255,.085);--knob-in:rgba(255,255,255,.06)}}}}
.card{{box-shadow:none}} .bk{{border-bottom:0;background:transparent;-webkit-backdrop-filter:none;backdrop-filter:none}}
.btn{{background:rgba(44,74,116,.12);box-shadow:none}}
</style></head><body>
<a class="bk" href="/_x/" onclick="if(history.length>1){{history.back();return false}}">‹ 返回</a>
<main>{body}</main></body></html>"""


class Web:
    def __init__(self, base: str, port: int, data_dir: Path, user: str = "", pw_hash: str = "", watch_host: str = ""):
        self.user, self.pw_hash = user, pw_hash
        self.fails: dict[str, list[float]] = {}
        self.all_fails: list[float] = []
        self.base = base.rstrip("/")
        self.port = port
        self.turns_dir = data_dir / "turns"
        self.store_path = data_dir / "web.json"
        self.push = Push(data_dir / "push.json", self.base or "https://localhost", mirror=watch_host)
        self.store = self._load()
        self._rewind_lock = asyncio.Lock()
        self.speech = Speech(data_dir / "speech")
        self.bridge = None  # 远窗本体（app.Bridge），网页对话框靠它看对话、发话
        self._threads: dict[int, tuple[tuple, list[dict]]] = {}
        self._zh_path = data_dir / "translations.json"
        try:
            self._zh: dict[str, str] = json.loads(self._zh_path.read_text())
        except (OSError, json.JSONDecodeError):
            self._zh = {}
        self._zh_busy: set[str] = set()
        self._uimg: dict[str, tuple[bytes, str]] = {}   # 你发过的图，最近取过的留在内存
        self._uimg_miss: set[str] = set()
        self._zh_failed: set[str] = set()
        self._search_cache: dict[int, tuple] = {}  # 译不出来的不再反复试（免得英文、“翻译中”来回闪）
        self.watch: dict[int, float] = {}  # 你正开着看的对话 → 网页最近一次报“我在看”的时间

    def watching(self, num: int) -> bool:
        """你正开着这个对话、页面在前台（网页每 15 秒报一次）：做完就不再弹通知、不发钉钉。"""
        return time.time() - self.watch.get(num, 0) < 35

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    # ---------- 登录 ----------
    def _load(self) -> dict:
        try:
            d = json.loads(self.store_path.read_text())
        except (OSError, json.JSONDecodeError):
            d = {}
        return {"sessions": d.get("sessions", {}), "devices": d.get("devices", {})}

    def _save(self) -> None:
        now = time.time()
        self.store["sessions"] = {k: v for k, v in self.store["sessions"].items() if v > now}
        self.store["devices"] = {k: v for k, v in self.store.get("devices", {}).items() if k in self.store["sessions"]}
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.store))
        tmp.chmod(0o600)
        tmp.replace(self.store_path)

    def link(self, target: str) -> str:
        """某个页面的固定网址（没登录会先到登录页，登完回到这里）。"""
        return f"{self.base}{target}"

    def _logged_in(self, request: web.Request) -> bool:
        sid = request.cookies.get(COOKIE, "")
        ok = bool(sid) and self.store["sessions"].get(sid, 0) > time.time()
        if ok:   # 记一下这台设备最近什么时候用过（5 分钟记一次，不每次写盘）
            dev = self.store.setdefault("devices", {}).setdefault(sid, {"created": time.time()})
            if time.time() - dev.get("seen", 0) > 300:
                dev.update(seen=time.time(), ua=request.headers.get("User-Agent", "")[:300], ip=self._client_ip(request))
                self._save()
        return ok

    @staticmethod
    def _client_ip(request: web.Request) -> str:
        # nginx 把真实来源追加在 X-Forwarded-For 最后；前面的可以被伪造，只认最后一个
        xff = request.headers.get("X-Forwarded-For", "")
        return xff.split(",")[-1].strip() if xff else (request.remote or "")

    def _locked(self, ip: str) -> int:
        """还要锁多少秒（0 = 没锁）。"""
        now = time.time()
        self.all_fails = [t for t in self.all_fails if now - t < LOCK_ALL[1]]
        mine = self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < LOCK_IP[1]]
        if len(self.all_fails) >= LOCK_ALL[0]:
            return int(self.all_fails[-LOCK_ALL[0]] + LOCK_ALL[1] - now) + 1
        if len(mine) >= LOCK_IP[0]:
            return int(mine[-LOCK_IP[0]] + LOCK_IP[1] - now) + 1
        return 0

    @staticmethod
    def _safe_next(target: str) -> str:
        return target if target.startswith("/") and not target.startswith("//") else f"{PREFIX}/"

    def _login_form(self, nxt: str, msg: str = "", status: int = 200) -> web.Response:
        note = f'<p class="err">{html.escape(msg)}</p>' if msg else ""
        body = LOGIN_CSS + f"""
<form class="login" method="post" action="{PREFIX}/login">
{MARK}<h1>远窗</h1><p class="sub">登录一次，这台设备 30 天免登录</p>{note}
<input type="hidden" name="next" value="{html.escape(nxt)}">
<input name="username" placeholder="账号" autocomplete="username" inputmode="numeric" required>
<input name="password" type="password" placeholder="密码" autocomplete="current-password" required>
<button>登录</button></form>"""
        return self._page("登录 · 远窗", body, status)

    async def _login_get(self, request: web.Request) -> web.Response:
        nxt = self._safe_next(request.query.get("next", f"{PREFIX}/"))
        if self._logged_in(request):
            raise web.HTTPFound(nxt)
        return self._login_form(nxt)

    async def _login_post(self, request: web.Request) -> web.Response:
        form = await request.post()
        nxt = self._safe_next(str(form.get("next", f"{PREFIX}/")))
        ip = self._client_ip(request)
        if wait := self._locked(ip):
            return self._login_form(nxt, f"错太多次了，{max(1, wait // 60)} 分钟后再试。", 429)
        ok = (self.pw_hash and hmac.compare_digest(str(form.get("username", "")).strip(), self.user)
              and check_password(str(form.get("password", "")), self.pw_hash))
        if not ok:
            now = time.time()
            self.fails.setdefault(ip, []).append(now)
            self.all_fails.append(now)
            log.warning("网页登录失败：%s", ip)
            return self._login_form(nxt, "账号或密码不对。", 401)
        self.fails.pop(ip, None)
        sid = secrets.token_urlsafe(32)
        self.store["sessions"][sid] = time.time() + SESSION_DAYS * 86400
        self.store.setdefault("devices", {})[sid] = {"created": time.time(), "seen": time.time(), "ip": ip,
                                                     "ua": request.headers.get("User-Agent", "")[:300]}
        self._save()
        resp = web.HTTPFound(nxt)
        resp.set_cookie(COOKIE, sid, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax",
                        secure=self.base.startswith("https://"), path="/")
        raise resp

    @staticmethod
    def _device_name(ua: str) -> str:
        """把浏览器标识说成人话：iPhone · Safari、Windows 电脑 · Edge……"""
        kind = ("iPad" if "iPad" in ua else "iPhone" if "iPhone" in ua else "安卓手机" if "Android" in ua
                else "Windows 电脑" if "Windows" in ua else "Mac" if "Macintosh" in ua else "更早登录的设备")
        app = ("Edge" if "Edg/" in ua else "微信" if "MicroMessenger" in ua else "钉钉" if "DingTalk" in ua
               else "Chrome" if ("Chrome/" in ua or "CriOS" in ua) else "Firefox" if "Firefox" in ua else "Safari" if "Safari" in ua
               else "主屏幕 app" if kind in ("iPhone", "iPad") else "")
        return f"{kind} · {app}" if app else kind

    def _device_id(self, sid: str) -> str:
        return hashlib.sha1(sid.encode()).hexdigest()[:12]   # 给网页看的编号，不露出真正的登录凭证

    async def _api_devices(self, request: web.Request) -> web.Response:
        """登录着的设备：哪台、最近什么时候用过、从哪个网络来，当前这台标出来。"""
        me = request.cookies.get(COOKIE, "")
        now = time.time()
        rows = []
        for sid, exp in self.store["sessions"].items():
            if exp <= now:
                continue
            d = self.store.get("devices", {}).get(sid, {})
            ip = d.get("ip", "")
            rows.append({"id": self._device_id(sid), "name": self._device_name(d.get("ua", "")), "me": sid == me,
                         "seen": d.get("seen") or d.get("created") or 0, "created": d.get("created") or 0,
                         "ip": ".".join(ip.split(".")[:2] + ["*", "*"]) if ip.count(".") == 3 else ip,
                         "push": bool(d.get("endpoint"))})
        rows.sort(key=lambda r: (not r["me"], -r["seen"]))
        return web.json_response({"devices": rows})

    async def _api_device_logout(self, request: web.Request) -> web.Response:
        """退出某一台（不能用来退出自己这台，那个走“退出登录”）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        want = str((await request.json()).get("id") or "")
        me = request.cookies.get(COOKIE, "")
        for sid in list(self.store["sessions"]):
            if self._device_id(sid) == want and sid != me:
                dev = self.store.get("devices", {}).pop(sid, {})
                self.store["sessions"].pop(sid, None)
                if dev.get("endpoint"):
                    self.push.remove(dev["endpoint"])
                self._save()
                return web.json_response({"ok": True})
        return web.json_response({"ok": False}, status=404)

    async def _api_logout(self, request: web.Request) -> web.Response:
        """退出登录：只退这台设备，或 all=true 时所有设备都退（手机丢了用）；这台设备的手机通知也一并停掉。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        data = await request.json()
        if data.get("all"):
            self.store["sessions"] = {}
            self.store["devices"] = {}
            self.push.subs = []
            self.push._save()
        else:
            self.store["sessions"].pop(request.cookies.get(COOKIE, ""), None)
            if data.get("endpoint"):
                self.push.remove(str(data["endpoint"]))
        self._save()
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

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
            response = await self._api_thread(request)
            items = json.loads(response.body)["items"]
            text = next((it["text"] for it in items if it.get("who") == "claude" and it.get("end") == data["end"]), "")
        if not text:
            return web.json_response({"ok": False, "error": "这条回复还未保存，请稍后再点朗读"})
        try:
            return web.json_response({"ok": True, **await self.speech.start(num, text, listener=self._speech_listener(request, data.get('listener', '')))})
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)})

    def _speech_allowed(self, key: str):
        try:
            m = self.speech.meta(key)
            session = self.bridge.registry.get(m["num"])
            if not session or session.hidden:
                raise web.HTTPNotFound()
        except (ValueError, KeyError):
            raise web.HTTPNotFound() from None

    async def _api_audio(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        self._speech_allowed(key)
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
        self._speech_allowed(key)
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

    # ---------- 记下一轮的完整过程 ----------
    def save_turn(self, label: str, turn, cwd: str, title: str) -> str:
        tid = f"{label.split()[0].lstrip('#')}-{int(turn.started)}"
        files = list(dict.fromkeys(turn.changed_files))
        # 交给你的截图和文件复制一份到远窗自己的文件夹（原处可能是临时目录，过后会被清掉）
        out = self.store_path.parent / "outbox" / tid
        kept_images, kept_files = [], []
        for src, dest in [(p, kept_images) for p in turn.images] + [(p, kept_files) for p in getattr(turn, "files", [])]:
            try:
                out.mkdir(parents=True, exist_ok=True)
                target = out / Path(src).name
                shutil.copy2(src, target)
                dest.append(str(target))
            except OSError:
                dest.append(src)
        rec = {
            "id": tid, "label": label, "title": title, "request": turn.request, "cwd": cwd,
            "started": turn.started, "ended": turn.ended, "schedule_id": turn.schedule_id, "schedule_run_id": turn.schedule_run_id, "checkpoint_before": turn.checkpoint_before, "checkpoint_after": turn.checkpoint_after, "codex_turn_id": turn.codex_turn_id, "model": turn.model, "effort": turn.effort,
            "final_text": turn.final_text, "error": turn.error, "timeline": turn.timeline,
            "changed_files": files, "commands": turn.commands, "images": kept_images, "files": kept_files,
            "translated": turn.translated,
        }
        self.turns_dir.mkdir(parents=True, exist_ok=True)
        (self.turns_dir / f"{tid}.json").write_text(json.dumps(rec, ensure_ascii=False))
        return tid

    def _turn(self, tid: str) -> dict | None:
        if not re.fullmatch(r"\d+-\d+", tid):
            return None
        try:
            return json.loads((self.turns_dir / f"{tid}.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    # ---------- 页面 ----------
    @staticmethod
    def _page(title: str, body: str, status: int = 200) -> web.Response:
        return web.Response(text=PAGE.format(title=html.escape(title), body=body), status=status,
                            content_type="text/html", headers={"Cache-Control": "no-store",
                                                               "Referrer-Policy": "no-referrer"})

    def _denied(self, request: web.Request) -> web.Response:
        if request.method == "GET" and "/api/" not in request.path:
            raise web.HTTPFound(f"{PREFIX}/login?next={quote(request.rel_url.path_qs, safe='')}")
        return web.json_response({"detail": "需要先登录远窗"}, status=401)

    async def _app_page(self, request: web.Request) -> web.Response:
        from .webui import app_html
        return web.Response(text=app_html(), content_type="text/html", headers={"Cache-Control": "no-store"})

    async def _icon(self, request: web.Request) -> web.StreamResponse:
        name = "icon-512.png" if request.path.endswith("512.png") else "icon.png"
        return web.FileResponse(Path(__file__).parent / "static" / name, headers={"Cache-Control": "max-age=86400"})

    async def _manifest(self, request: web.Request) -> web.Response:
        return web.json_response({"name": "远窗", "short_name": "远窗", "start_url": f"{PREFIX}/", "display": "standalone",
                                  "background_color": "#d5dee5", "theme_color": "#d5dee5",
                                  "icons": [{"src": f"{PREFIX}/icon.png", "sizes": "180x180", "type": "image/png"},
                                            {"src": f"{PREFIX}/icon-512.png", "sizes": "512x512", "type": "image/png"}]})

    async def _sw(self, request: web.Request) -> web.Response:
        # 点通知：先把要打开的对话编号记进缓存（iPhone 冷启动时不一定带上地址），再叫醒已开着的页面或新开一个
        js = """self.addEventListener('push', e => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(self.registration.showNotification(d.title || '远窗', {body: d.body || '', tag: d.tag || undefined, data: {num: d.num || null}, icon: '/_x/icon.png'}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const n = e.notification.data && e.notification.data.num;
  e.waitUntil((async () => {
    if (n) await (await caches.open('anzhuo')).put('/_x/__open', new Response(String(n)));
    const cs = await clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of cs) { if (n) c.postMessage({open: n}); if ('focus' in c) return c.focus(); }
    return clients.openWindow(n ? '/_x/?s=' + n : '/_x/');
  })());
});
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
"""
        return web.Response(text=js, content_type="application/javascript", headers={"Cache-Control": "no-cache"})

    async def _health(self, request: web.Request) -> web.Response:
        # 外网服务器那边每分钟来问一声“还在吗”（scripts/server_watch.py）；网页也用它判断 Mac 连不连得上。不带任何内容
        return web.json_response({"ok": True})

    async def _api_push_key(self, request: web.Request) -> web.Response:
        return web.json_response({"key": await asyncio.to_thread(self.push.public_key)})

    async def _api_push(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        try:
            sub = await request.json()
            self.push.add(sub)
            sid = request.cookies.get(COOKIE, "")
            if sid in self.store["sessions"]:
                self.store.setdefault("devices", {}).setdefault(sid, {})["endpoint"] = sub.get("endpoint")
                self._save()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        return web.json_response({"ok": True})

    async def _to_index(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{PREFIX}/")

    async def _index(self, request: web.Request) -> web.Response:
        """历史记录：按对话列（不是按每一轮）。点对话名回到那个对话接着聊；展开“每一轮”看各轮的完整过程页。"""
        convs: dict[int, list[dict]] = {}
        for p in self.turns_dir.glob("*.json"):
            try:
                r = json.loads(p.read_text())
                convs.setdefault(int(r["id"].split("-")[0]), []).append(r)
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
        reg = getattr(self.bridge, "registry", None) if self.bridge else None
        last = lambda r: r.get("ended") or r.get("started") or 0
        rows = []
        for num, rs in sorted(convs.items(), key=lambda kv: -max(map(last, kv[1])))[:60]:
            rs.sort(key=last, reverse=True)
            s = reg.get(num) if reg else None
            name = s.name if s else re.sub(r"^#\d+\s*", "", rs[0].get("label", "")) or f"#{num}"
            bits = [f"{len(rs)} 轮", time.strftime("%m-%d %H:%M", time.localtime(last(rs[0]))), rs[0].get("title", "")]
            if s and s.hidden:
                bits.append("已删除")
            turns = "".join(
                f'<li><a href="{PREFIX}/t/{html.escape(r["id"])}">{time.strftime("%m-%d %H:%M", time.localtime(last(r)))} · '
                f'{html.escape(r.get("request") or r.get("title") or "")}</a></li>' for r in rs[:30])
            more = f'<li class="meta">更早的 {len(rs) - 30} 轮略</li>' if len(rs) > 30 else ""
            rows.append(f'<div class="card"><a href="{PREFIX}/?s={num}"><b>{html.escape(name)}</b></a>'
                        f'<div class="meta">{html.escape(" · ".join(b for b in bits if b))}</div>'
                        f'<details><summary class="meta">每一轮</summary><ul>{turns}{more}</ul></details></div>')
        return self._page("远窗 · 历史记录", "<h1>历史记录</h1><p class='meta'>按对话列，最近动过的在前；点名字回到那个对话。</p>"
                          + ("".join(rows) or "<p class='meta'>还没有记录。</p>"))

    async def _turn_page(self, request: web.Request) -> web.Response:
        r = self._turn(request.match_info["tid"])
        if not r:
            return self._page("没有这一轮", "<h1>没有这一轮的记录</h1>", 404)
        mins = max(1, round(((r["ended"] or r["started"]) - r["started"]) / 60))
        when = time.strftime("%m-%d %H:%M", time.localtime(r["started"]))
        out = [f'<p class="meta"><a href="{PREFIX}/history">← 历史记录</a></p>',
               f"<h1>{html.escape(r['title'])}：{html.escape(r['request'] or r['label'])}</h1>",
               f"<p class='meta'>{html.escape(r['label'])} · {when} 起 · {mins} 分钟 · "
               f"{html.escape(display(Path(r['cwd'])))} · {html.escape(r.get('model') or '')}</p>"]
        if r.get("error"):
            out.append(f"<div class='card'><b>出错原因</b>：{html.escape(r['error'])}</div>")
        if r.get("final_text"):
            out.append(f"<h2>结果</h2><div class='card say'>{_md(r['final_text'])}</div>")
        if r.get("images"):
            out.append("<h2>截图</h2>" + "".join(f'<img src="{PREFIX}/t/{r["id"]}/img/{i}" loading="lazy">'
                                                for i in range(len(r["images"]))))
        if r.get("files"):
            out.append("<h2>交给你的文件</h2><ul>" + "".join(
                f'<li><a href="{PREFIX}/t/{r["id"]}/file/{i}">{html.escape(Path(f).name)}</a></li>'
                for i, f in enumerate(r["files"])) + "</ul>")
        if r.get("changed_files"):
            out.append("<h2>改了哪些文件</h2><ul>" + "".join(
                f"<li><code>{html.escape(display(Path(f)))}</code></li>" for f in r["changed_files"]) + "</ul>")
        steps = []
        for e in r.get("timeline", []):
            t = time.strftime("%H:%M:%S", time.localtime(e["t"]))
            if e["kind"] == "you":
                steps.append(f"<div class='step'><span class='t'>{t}</span><div><b>你插话</b>：{_md(e['text'])}</div></div>")
            elif e["kind"] == "say":
                steps.append(f"<div class='step'><span class='t'>{t}</span><div class='say'>{_md(e['text'])}</div></div>")
            else:
                detail = e.get("detail") or ""
                inner = (f"<details><summary>{html.escape(e['text'])}</summary><pre>{html.escape(detail)}</pre></details>"
                         if detail and detail != e["text"] else html.escape(e["text"]))
                steps.append(f"<div class='step'><span class='t'>{t}</span><div>{inner}</div></div>")
        if steps:
            out.append(f"<h2>完整过程（{len(steps)} 条）</h2><div class='card'>{''.join(steps)}</div>")
        return self._page(f"{r['label']} · {r['request'] or ''}", "".join(out))

    async def _turn_image(self, request: web.Request) -> web.StreamResponse:
        r = self._turn(request.match_info["tid"])
        i = int(request.match_info["i"])
        if not r or not 0 <= i < len(r.get("images", [])) or not Path(r["images"][i]).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(r["images"][i], headers={"Cache-Control": "private, max-age=3600"})

    async def _turn_file(self, request: web.Request) -> web.StreamResponse:
        r = self._turn(request.match_info["tid"])
        i = int(request.match_info["i"])
        if not r or not 0 <= i < len(r.get("files", [])) or not Path(r["files"][i]).is_file():
            raise web.HTTPNotFound()
        name = Path(r["files"][i]).name
        inline = Path(name).suffix.lower() == ".pdf" and request.query.get("download") != "1"
        return web.FileResponse(r["files"][i], headers={
            "Cache-Control": "private, max-age=3600",
            **({"Content-Type": "application/pdf"} if inline else {}),
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}"})

    # ---------- PDF 在手机上看：转成一页页图片（iPhone 的网页里直接放 PDF 只显示第一页、还缩不全） ----------
    def _pdf_dir(self, request: web.Request) -> tuple[Path, Path]:
        r = self._turn(request.match_info["tid"])
        i = int(request.match_info["i"])
        if not r or not 0 <= i < len(r.get("files", [])):
            raise web.HTTPNotFound()
        src = Path(r["files"][i])
        if src.suffix.lower() != ".pdf" or not src.is_file():
            raise web.HTTPNotFound()
        return src, self.store_path.parent / "pdfpages" / f"{r['id']}-{i}"

    async def _pdf_pages(self, request: web.Request) -> web.Response:
        src, out = self._pdf_dir(request)
        if not any(out.glob("p-*.png")):
            out.mkdir(parents=True, exist_ok=True)
            exe = shutil.which("pdftoppm") or str(Path.home() / ".local/bin/pdftoppm")
            proc = await asyncio.create_subprocess_exec(exe, "-png", "-scale-to", "1400", "-l", "80", str(src), str(out / "p"),
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), 120)
            except asyncio.TimeoutError:
                proc.kill()
        pages = sorted(out.glob("p-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        base = f"/dl/{request.match_info['tid']}/{request.match_info['i']}/p/"
        return web.json_response({"pages": [base + p.name for p in pages]})

    async def _pdf_page(self, request: web.Request) -> web.StreamResponse:
        _, out = self._pdf_dir(request)
        name = request.match_info["name"]
        if not re.fullmatch(r"p-\d+\.png", name) or not (out / name).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(out / name, headers={"Cache-Control": "private, max-age=86400"})

    # ---------- 网页对话框的接口 ----------
    def _turn_paths(self, num: int) -> list[Path]:
        s = self.bridge.registry.get(num) if self.bridge and hasattr(self.bridge, "registry") else None
        inherited = getattr(s, "codex_records", [])
        hidden = set(getattr(s, "codex_hidden", []))
        own = sorted(self.turns_dir.glob(f"{num}-*.json"), key=lambda p: int(p.stem.split("-")[1]))
        return [self.turns_dir / (tid + ".json") for tid in inherited if re.fullmatch(r"\d+-\d+", tid) and tid not in hidden] + [p for p in own if p.stem not in hidden]

    async def _codex_target(self, s, to: str) -> str:
        records = self._records(s.num)
        rec = next((r for r in records if r["id"] == to), None)
        if not rec or not s.codex_id:
            raise ValueError("找不到那条回复，原对话未改动")
        if rec.get("codex_turn_id"):
            return rec["codex_turn_id"]
        # 老记录没有原生轮次编号时，只按唯一、接近的开始时间核验，不猜序号。
        from .codex import CodexConn
        conn = CodexConn(s.cwd, lambda *a: None)
        try:
            await conn.start()
            result = await conn.request("thread/read", {"threadId": s.codex_id, "includeTurns": True})
            hits = [t["id"] for t in (result.get("thread") or {}).get("turns", [])
                    if t.get("startedAt") and (rec.get("started") or 0) - 2 <= t["startedAt"] <= (rec.get("ended") or rec.get("started") or 0) + 2]
            if len(hits) != 1:
                raise ValueError("这条旧回复无法准确定位到 Codex 轮次，原对话未改动")
            return hits[0]
        finally:
            await conn.close()

    def _records(self, num: int) -> list[dict]:
        out = []
        for p in self._turn_paths(num):
            try:
                r = json.loads(p.read_text())
                fs = [{"name": Path(f).name, "size": Path(f).stat().st_size if Path(f).is_file() else 0,
                       "url": f"/dl/{r['id']}/{i}"} for i, f in enumerate(r.get("files", []))]
                out.append({"id": r["id"], "title": r.get("title", ""), "started": r.get("started"), "ended": r.get("ended"), "files": fs,
                            "checkpoint_before": r.get("checkpoint_before", ""), "checkpoint_after": r.get("checkpoint_after", ""), "codex_turn_id": r.get("codex_turn_id", ""), "model": r.get("model", ""), "effort": r.get("effort", ""), "nfiles": len(r.get("changed_files") or []), "ncmds": len(r.get("commands") or []),
                            "images": [f"{PREFIX}/t/{r['id']}/img/{i}" for i in range(len(r.get("images", [])))],
                            "final_text": r.get("final_text", ""), "translated": r.get("translated", "")})
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return out

    def _codex_history(self, num: int) -> list[dict]:
        """Codex 对话的一来一回：Codex 的记录格式不一样，直接用远窗自己记的每轮（你说的话 + 过程 + 最后的回复）。"""
        items: list[dict] = []
        for p in self._turn_paths(num):
            try:
                r = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            tl = r.get("timeline") or []
            said = [e["text"] for e in tl if e.get("kind") == "you"] or [r.get("request") or ""]
            items += [{"who": "you", "text": t} for t in said if t]
            proc = [{"k": e["kind"], "text": str(e.get("text", ""))[:1500], **({"detail": e["detail"]} if e.get("detail") else {}), **({"sub": e["sub"]} if e.get("sub") else {})}
                    for e in tl if e.get("kind") in ("think", "say", "do")]
            if proc and proc[-1]["k"] == "say" and proc[-1]["text"] == (r.get("final_text") or "")[:1500]:
                proc.pop()
            items.append({"who": "claude", "end": r["id"], "ts": r.get("ended") or r.get("started"), "text": r.get("final_text") or r.get("error") or "（没有回复）",
                          "steps": sum(1 for e in tl if e.get("kind") == "do"), "process": proc[-80:]})
        lt = getattr(self.bridge.runner, "last", {}).get(num) if self.bridge else None
        session = self.bridge.registry.get(num) if self.bridge and hasattr(self.bridge, "registry") else None
        if lt and f"{num}-{int(lt.started)}" not in getattr(session, "codex_hidden", []) and not any(p.stem == f"{num}-{int(lt.started)}" for p in self.turns_dir.glob(f"{num}-*.json")):
            # 刚做完、记录还没写好（翻译、存档要几秒）：先用这一轮本身显示，免得回复“消失”一下
            items += [{"who": "you", "text": e["text"]} for e in lt.timeline if e.get("kind") == "you"]
            items.append({"who": "claude", "text": lt.final_text or lt.error or "（没有回复）",
                          "ts": lt.ended, "elapsed": max(0, lt.ended - lt.started) if lt.ended >= lt.started else None,
                          "steps": len(lt.steps), "process": []})
        return items[-300:]

    async def _api_sessions(self, request: web.Request) -> web.Response:
        b = self.bridge
        now = time.time()
        rows = []
        for s in [x for x in b.registry.sessions.values() if not x.hidden]:
            turn = b.runner.turns.get(s.num)
            if turn and turn.confirm:
                status, detail = "awaiting", f"等你确认：{turn.confirm['what']}"
            elif turn and b.runner.compacting(s.num) == "running":
                status, detail = "running", "正在压缩上下文"
            elif turn:
                status, detail = "running", f"在做 · {int((now - turn.started) // 60)} 分钟 · 第 {len(turn.steps)} 步"
            elif b.runner.busy(s.num):
                status, detail = "queued", "排队中"
            elif s.limit_until > now:
                status = "paused"
                detail = f"额度用完 · {time.strftime('%H:%M', time.localtime(s.limit_until))} 恢复" + ("后接着做" if s.auto_resume else "")
            elif s.awaiting and s.awaiting_kind == "suggest":
                status, detail = "idle", f"有建议：{s.awaiting}"
            elif s.awaiting:
                status, detail = "awaiting", f"等你定：{s.awaiting}"
            elif s.status == "error":
                status, detail = "error", "上次出错了"
            else:
                status, detail = "idle", s.last_title or "空闲"
            unread = status in ("idle", "awaiting", "error", "paused") and bool(s.claude_id or s.codex_id) and s.updated > s.seen + 1
            rows.append({"num": s.num, "name": s.name, "cwd": display(Path(s.cwd)), "status": status, "unread": unread,
                         "detail": detail, "updated": s.updated, "steps": len(turn.steps) if turn else 0,
                         "engine": getattr(s, "engine", "claude"),
                         "pinned": s.pinned,
                         "suggest": s.awaiting_kind == "suggest" and status == "idle",
                         "last_step": turn.steps[-1] if turn and turn.steps else ""})
        order = {"awaiting": 0, "running": 1, "queued": 2, "error": 3, "paused": 4, "idle": 5}
        rows.sort(key=lambda r: (order[r["status"]], -r["updated"]))
        return web.json_response({"sessions": rows, "recent": self._recent_folders(), "last_folder": self._last_folder(),
                                  "folders": list(b.cfg.folders), "current": b.registry.current, "models": model_options(),
                                  "limits": self._limits(b.runner.limits),
                                  "limits_gpt": self._limits(getattr(b.runner, "limits_gpt", {})),
                                  "has_gpt": any(getattr(x, "engine", "") == "codex" for x in b.registry.visible())})

    def _recent_folders(self, limit: int = 6) -> list[dict]:
        """最近用过的工作文件夹（新对话菜单里排在最前）；临时文件夹和家目录不算。这里不碰磁盘：受保护的文件夹一 stat 就可能卡住。"""
        b = self.bridge
        scratch = str(Path(b.cfg.scratch_root).expanduser())
        home = str(Path.home())
        seen, out = {str(Path(v).expanduser()) for v in b.cfg.folders.values()}, []   # 配了短名的文件夹已经在菜单里，不再重复列
        for s in sorted(b.registry.sessions.values(), key=lambda x: -x.updated):
            p = str(Path(s.cwd).expanduser())
            if p in seen or p == home or p.startswith(scratch):
                continue
            seen.add(p)
            out.append({"path": p, "name": Path(p).name or p, "show": display(Path(p))})
            if len(out) >= limit:
                break
        return out

    def _last_folder(self) -> str:
        """新对话默认选哪个文件夹：最近一个对话用的（有短名就给短名，没有给路径）；没有、或它用的是临时文件夹，就空（前端落到“临时文件夹”）。"""
        b = self.bridge
        scratch, home = str(Path(b.cfg.scratch_root).expanduser()), str(Path.home())
        for s in sorted(b.registry.sessions.values(), key=lambda x: -x.updated):
            p = str(Path(s.cwd).expanduser())
            if p == home or p.startswith(scratch):
                return ""
            return next((k for k, v in b.cfg.folders.items() if str(Path(v).expanduser()) == p), p)
        return ""

    @staticmethod
    def _limits(d: dict) -> list[dict]:
        def epoch(text):
            m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日 (\d{1,2}):(\d{2})", text or "")
            if not m:
                return None
            now = time.time()
            year = time.localtime(now).tm_year
            candidates = []
            for y in (year - 1, year, year + 1):
                try:
                    from datetime import datetime
                    date = datetime(y, *map(int, m.groups()))
                    candidates.append(date.timestamp())
                except ValueError:
                    pass
            return min(candidates, key=lambda t: abs(t - now)) if candidates else None
        return [{"name": LIMIT_CN.get(k, k), "pct": round(u * 100), "reset": r or "", "reset_at": epoch(r)} for k, (u, r) in d.items()]

    async def _api_manage(self, request: web.Request) -> web.Response:
        """列表里长按一条对话：置顶 / 取消置顶、改名、归类、删除（收起）、找回。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        s = b.registry.get(int(request.match_info["num"]))
        if not s:
            raise web.HTTPNotFound()
        data = await request.json()
        act, val = data.get("action"), str(data.get("value") or "").strip()
        if act == "pin":
            s.pinned = bool(data.get("value"))
        elif act == "rename":
            if not val:
                return web.json_response({"ok": False, "error": "名字不能空着"})
            s.name, s.renamed = val[:40], True
        elif act == "delete":
            if s.num in b.runner.turns or b.runner.busy(s.num):
                return web.json_response({"ok": False, "error": "还在做，先停下再删"})
            await self.speech.remove_session(s.num)
            s.hidden, s.hidden_at, s.pinned = True, time.time(), False
            if b.registry.current == s.num:
                b.registry.current = None
        elif act == "autoresume":
            s.auto_resume = bool(data.get("value"))
            if s.auto_resume and s.limit_until > time.time():
                asyncio.create_task(b._auto_resume(s.num, s.limit_until))
        elif act == "dismiss":   # 不想回答了：把“等你定 / 出错了”的状态撤掉，对话回到空闲
            if s.num in b.runner.turns or b.runner.busy(s.num):
                return web.json_response({"ok": False, "error": "还在做，没有可撤的状态"})
            s.awaiting = s.awaiting_kind = ""
            if s.status == "error":
                s.status = "idle"
            s.seen = time.time()
            self._threads.pop(s.num, None)
        elif act == "restore":
            s.hidden, s.hidden_at = False, 0.0
            s.updated = max(s.updated, time.time() - 60)   # 找回来的排在最近里
        else:
            raise web.HTTPBadRequest()
        b.registry.save()
        return web.json_response({"ok": True})

    def _search_items(self, s) -> list[dict]:
        """一个对话里能搜的话（你说的、它每轮的回复），按记录文件变没变缓存住，不用每次重读几十 MB 的记录。"""
        if s.engine == "codex":
            key = ("x", len(list(self.turns_dir.glob(f"{s.num}-*.json"))))
        else:
            p = thread.transcript_path(s.claude_id) if s.claude_id else None
            key = ("c", p.stat().st_mtime if p else 0)
        hit = self._search_cache.get(s.num)
        if hit and hit[0] == key:
            return hit[1]
        items: list[dict] = []
        try:
            if s.engine == "codex":
                items = self._codex_history(s.num)
            elif s.claude_id:
                items = thread.history(thread.full_messages(s.claude_id, s.cwd), limit=100000)
        except Exception:  # noqa: BLE001 - 读不到就当没有
            log.debug("搜索读记录失败", exc_info=True)
        items = [{"who": it["who"], "text": it.get("text") or ""} for it in items if it.get("who") in ("you", "claude")]
        self._search_cache[s.num] = (key, items)
        return items

    async def _api_browse(self, request: web.Request) -> web.Response:
        """文件夹浏览（新对话“在哪做”的“浏览…”用）：只列子目录名，不读任何文件内容。"""
        raw = request.query.get("path", "").strip()
        if raw == "__drives__":   # “此电脑”层：所有盘符的列表（Windows）；文件系统里没有统一根，这层是虚拟的
            drives = [f"{d}\\" for d in os.listdrives()] if hasattr(os, "listdrives") else []
            return web.json_response({"raw": raw, "show": "此电脑", "parent": "",
                                      "dirs": drives, "absolute": True, "drive_level": True})
        try:
            base = Path(raw).expanduser() if raw else Path.home()
            if not base.is_absolute():
                base = Path.home() / base
            base = base.resolve()
            def visible(e):
                if e.name.startswith("."):
                    return False
                try:
                    return not (getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x2)   # Windows 隐藏属性
                except OSError:
                    return False
            dirs = sorted((e.name for e in base.iterdir() if e.is_dir() and visible(e)),
                          key=str.lower)[:500]
        except OSError:
            return web.json_response({"error": "读不到这个文件夹"}, status=400)
        at_root = base == base.parent
        return web.json_response({"raw": str(base), "show": display(base),
                                  "parent": "" if at_root else str(base.parent),
                                  "dirs": dirs, "drive_root": at_root and bool(base.drive)})

    @staticmethod
    def _cc_list(d: Path | None, limit: int | None) -> list:
        from claude_agent_sdk import list_sessions
        return list_sessions(str(d) if d else None, limit, 0, False)

    async def _api_cc_sessions(self, request: web.Request) -> web.Response:
        """电脑端（终端 / IDE / 桌面端）开的 Claude Code 对话，接着聊用：读 ~/.claude/projects 的记录，不碰文件内容。
        不带 path 列所有文件夹最近的，规则同钉钉的“接着桌面”：远窗自己开的、临时文件夹和家目录里的不算；带 path 只列那个文件夹的。"""
        raw = request.query.get("path", "").strip()
        d = Path(raw).expanduser() if raw else None
        if d and (not d.is_absolute() or not d.is_dir()):
            return web.json_response({"error": "先选一个文件夹"}, status=400)
        try:
            infos = await asyncio.to_thread(self._cc_list, d, 30 if d else 150)
        except Exception as e:  # noqa: BLE001 - SDK 读记录失败就算了，不让网页 500
            return web.json_response({"error": f"读不到对话记录：{e}"}, status=400)
        sessions = self.bridge.registry.sessions.values() if self.bridge else []
        mine = {s.claude_id: s.num for s in sessions if s.claude_id}
        if not d:
            ours = {s.claude_id for s in sessions if s.claude_id and not s.from_desktop}
            cfg = getattr(self.bridge, "cfg", None)
            scratch = Path(cfg.scratch_root).expanduser() if cfg else None
            infos = [x for x in infos if x.session_id not in ours and x.cwd and Path(x.cwd) != Path.home()
                     and not (scratch and Path(x.cwd).is_relative_to(scratch))][:30]
        out = [{"id": x.session_id,
                "title": (x.custom_title or x.summary or x.first_prompt or "（没标题）").replace("\n", " ")[:60],
                "at": x.last_modified, "num": mine.get(x.session_id),
                "cwd": x.cwd or (str(d) if d else "")} for x in infos]
        return web.json_response({"sessions": out})

    async def _api_resume_cc(self, request: web.Request) -> web.Response:
        """把 Claude Code 已有的对话接进远窗：建一个带 claude_id 的对话，之后的说话都 resume 它。
        已经接过的（包括收起的）直接回到原来那个，不另开一个：两个对话续同一份记录会互相打架。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        d = await request.json()
        path, sid = str(d.get("path", "")).strip(), str(d.get("session_id", "")).strip()
        name = str(d.get("name", "")).strip()[:16] or "接着聊"
        folder = Path(path).expanduser() if path else None
        if not folder or not folder.is_absolute() or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", sid):
            return web.json_response({"error": "参数不完整"}, status=400)
        if s := next((v for v in b.registry.sessions.values() if v.claude_id == sid), None):
            if s.hidden:
                s.hidden, s.hidden_at = False, 0.0
                s.updated = max(s.updated, time.time() - 60)
                b.registry.save()
            return web.json_response({"num": s.num, "name": s.name, "existing": True})
        if not folder.is_dir():
            return web.json_response({"error": "这段对话的文件夹已经不在了"}, status=400)
        try:
            found = {x.session_id: x for x in await asyncio.to_thread(self._cc_list, folder, None)}
        except Exception:  # noqa: BLE001
            found = {}
        if sid not in found:   # 只接这个文件夹里真有的对话
            return web.json_response({"error": "这个文件夹里没有这段对话"}, status=400)
        # 几分钟内还有人写这段记录：电脑上那个窗口多半还开着。两边各自记着自己的上下文，接着用会分叉，先提醒一句
        idle = time.time() - (found[sid].last_modified or 0) / 1000
        if idle < DESK_FRESH and not d.get("force"):
            return web.json_response({"warn": True, "ago": max(0, int(idle))})
        s = b.registry.add(Session(num=b.registry.reserve_num(), name=name,
                                   cwd=str(folder), claude_id=sid, from_desktop=True))
        return web.json_response({"num": s.num, "name": s.name})

    async def _api_search(self, request: web.Request) -> web.Response:
        q = request.query.get("q", "").strip()
        b = self.bridge
        if not q:
            return web.json_response({"q": q, "title": [], "said": [], "deleted": []})
        ql, now = q.lower(), time.time()
        out: dict[str, list] = {"title": [], "said": [], "deleted": []}
        for s in sorted(b.registry.sessions.values(), key=lambda x: -x.updated):
            row = {"num": s.num, "name": s.name, "updated": s.updated,
                   "hidden": s.hidden, "hidden_at": s.hidden_at, "engine": s.engine}
            if ql in s.name.lower():
                out["deleted" if s.hidden else "title"].append(row)
                continue
            items = await asyncio.to_thread(self._search_items, s)
            for it in reversed(items):
                i = it["text"].lower().find(ql)
                if i >= 0:
                    t = it["text"]
                    row["who"] = "you" if it["who"] == "you" else "ai"
                    row["snippet"] = ("…" if i > 24 else "") + t[max(0, i - 24):i + len(q) + 40].replace("\n", " ") + ("…" if i + len(q) + 40 < len(t) else "")
                    out["deleted" if s.hidden else "said"].append(row)
                    break
        return web.json_response({"q": q, **{k: v[:30] for k, v in out.items()}})

    async def _api_thread(self, request: web.Request) -> web.Response:
        b = self.bridge
        num = int(request.match_info["num"])
        s = b.registry.get(num)
        if not s:
            raise web.HTTPNotFound()
        # 两种引擎的附件都在完成后的归档里；归档可能晚于状态更新，须参与缓存失效。
        lt = b.runner.last.get(num) if s.engine == "codex" and hasattr(b.runner, "last") else None
        key = (s.claude_id or s.codex_id, s.updated, len(list(self.turns_dir.glob(f"{num}-*.json"))),
               lt.ended if lt else 0, self._cc_mtime(s))
        cached = self._threads.get(num)
        if cached and cached[0] == key:
            items = cached[1]
        else:
            items = []
            if s.engine == "codex":
                items = thread.attach_records(self._codex_history(num), self._records(num))
            elif s.claude_id:
                try:
                    times: dict = {}
                    msgs = await asyncio.to_thread(thread.full_messages, s.claude_id, s.cwd, times)
                    items = thread.attach_records(thread.history(msgs, times=times), self._records(num))
                except Exception:  # noqa: BLE001 - 读不到记录就只显示进行中的
                    log.debug("读对话记录失败", exc_info=True)
            self._threads[num] = (key, items)
        items = [dict(it) for it in items]
        # 你刚发的话：Claude 的记录要等这一轮做完才重读，先把还没出现在记录里的接在后面（不限时，进了记录才拿掉）
        # 带图的话在记录里是“[图片]\n原话”，比对时去掉图片占位、只看原话开头
        # 记录里你的话后面可能多了“（附件已存到：…）”，所以按开头比：记录里那句以你刚发的话开头就算进了记录
        said = [re.sub(r"^\[(?:\d+ 张)?图片\]\s*", "", it["text"].strip()) for it in items if it["who"] == "you"]
        out = b.outgoing.get(num, [])
        out[:] = [o for o in out if not any(x.startswith(o["text"].strip()[:40]) for x in said)]
        if s.rewind_at:   # 点过“回到这里”：那条之后的先收起来，下一句从那里接着；还没说下一句前能撤销
            cut = next((i for i, it in enumerate(items) if it.get("end") == s.rewind_at), None)
            if cut is not None:
                items = items[:cut + 1] + [{"who": "note", "rewind": True, "fork": s.forking, "text": s.rewind_note or "已回到这里"}]
        items += [{"who": "you", "text": o["text"], "pending": True} for o in out]
        for it in items:
            if it["who"] == "claude":
                it["text"] = self._zh_or_later(it["text"])
        for it in [i for i in items if i["who"] == "claude"][-2:]:   # 最近两轮的工作过程也译（再早的不译，太费）
            if it.get("process"):
                it["process"] = [self._zh_proc(e) if e["k"] in ("think", "say") else e
                                 for e in it["process"]]
        turn = b.runner.turns.get(num)
        if not turn and s.updated > s.seen:   # 打开了、看到最新一轮了，就不算没看
            s.seen = time.time()
            b.registry.save()
        live = None
        if turn:
            proc = [{"k": e["kind"], "text": str(e.get("text", ""))[:1200], "detail": str(e.get("detail") or "")[:400], "sub": e.get("sub", "")}
                    for e in turn.timeline if e["kind"] in ("think", "say", "do")][-40:]
            for e in proc:
                if e["k"] in ("think", "say"):
                    e.update(self._zh_proc(e))
            live = {"request": turn.request, "minutes": int((time.time() - turn.started) // 60),
                    "elapsed": int(time.time() - turn.started), "idle": int(time.time() - (turn.last_at or turn.started)),
                    "total": len(turn.steps), "steps": turn.steps[-8:], "injected": turn.injected, "process": proc,
                    "confirm": {"what": turn.confirm["what"], "cmd": turn.confirm["cmd"]} if turn.confirm else None,
                    "compact": b.runner.compacting(num) == "running",
                    "plan": turn.plan,
                    "agents": [{"name": a["name"], "kind": a["kind"], "status": a["status"], "last": a["last"],
                                "secs": int((a["t1"] or time.time()) - a["t0"])} for a in turn.agents.values()][-8:]}
        # 后台助手做完时它会再答一句空话（No response requested.），那句不算；跳过它找真正问你的那条
        last = next((it["text"] for it in reversed(items) if it["who"] == "claude" and not NOISE.match(it["text"] or "")), "")
        d = parse_decision(last, loose=True) if s.awaiting and not turn else None
        decision = {"question": d.question, "options": d.options, "recommend": d.recommend} if d else None
        recs = self._records(num)
        mid = (turn.model if turn and turn.model else "") or (recs[-1].get("model") if recs else "") or s.model or ""
        eff = s.effort or b.runner.default_effort
        pick = s.model or mid
        # 选择器展示接下来要用的型号；历史轮次仍保留那轮的实际型号。
        selected = mid if s.engine == "codex" else s.model or mid
        if s.engine == "codex" and display_model(selected) in ("", "GPT"):
            selected = dict(model_options())["gpt"]
        for item in items:
            if item.get("model"):
                item["model_label"] = display_model(item["model"])
                item["effort_label"] = EFFORT_CN.get(item.get("effort"), "")
        usage = {"model": display_model(selected) if selected else "默认模型", "effort": EFFORT_CN.get(eff, eff or "默认"),
                 "mkey": "gpt" if s.engine == "codex" else next((k for k in ("opus", "sonnet", "haiku", "fable") if k in pick.lower()), ""),
                 "ekey": eff or "",
                 "context": round(s.context_pct) if s.context_pct is not None else None,
                 "compacting": b.runner.compacting(num), "busy": b.runner.busy(num)}
        queue = ["压缩上下文" if j.text == COMPACT else (t[:80] or "（图片）") for j in b.runner.queued(num) for t in (j.parts or [j.text])]
        limit = {"until": s.limit_until, "at": time.strftime("%H:%M", time.localtime(s.limit_until)), "auto": s.auto_resume} \
            if s.limit_until > time.time() and not turn else None
        five = b.runner.limits.get("five_hour")
        return web.json_response({"num": num, "name": s.name, "cwd": display(Path(s.cwd)), "items": items,
                                  "live": live, "queue": queue, "awaiting": "" if turn else s.awaiting,
                                  "awaiting_kind": "" if turn else s.awaiting_kind, "decision": decision, "usage": usage,
                                  "limit": limit, "engine": s.engine,
                                  "five_hour": {"pct": round(five[0] * 100), "reset": five[1] or ""} if five else None})

    def _zh_or_later(self, text: str) -> str:
        """英文的先原样给，后台译好存起来，下次刷新就是中文。"""
        if not translate.needs_translation(text):
            return text
        key = hashlib.sha1(text.encode()).hexdigest()
        if key in self._zh:
            return self._zh[key]
        if key not in self._zh_busy and key not in self._zh_failed and len(self._zh_busy) < 6:
            self._zh_busy.add(key)
            asyncio.create_task(self._translate(key, text))
        return text

    def _zh_proc(self, e: dict) -> dict:
        """工作过程里的一条思考 / 说明：正在译的先不给英文（网页显示“翻译中…”），译好一次出中文，读着不会突然换字。"""
        text = self._zh_or_later(e["text"])
        if text == e["text"] and hashlib.sha1(text.encode()).hexdigest() in self._zh_busy:
            return dict(e, text="", wait=True)
        return dict(e, text=text)

    async def _translate(self, key: str, text: str) -> None:
        """网页里显示的英文回复后台译成中文（和钉钉同一套：DeepSeek，Haiku 兜底），译好存起来，下次刷新就是中文。"""
        try:
            if done := await translate.to_chinese(text):
                self._zh[key] = done[0]
            else:
                self._zh_failed.add(key)
                self._zh = dict(list(self._zh.items())[-2000:])
                self._zh_path.write_text(json.dumps(self._zh, ensure_ascii=False))
        finally:
            self._zh_busy.discard(key)

    def _same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        return origin is None or origin == self.base or not self.base.startswith("https://")

    async def _save_files(self, data: dict) -> list[str]:
        """网页传上来的文件（任何格式）存进收件箱，原文件名保留在后面。"""
        files = data.get("files") or []
        paths = []
        folder = Path(self.store_path.parent) / "inbox" / "web"
        folder.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(files):
            name = Path(str(f.get("name") or "file")).name.replace("/", "_")[:120] or "file"
            p = folder / f"{int(time.time() * 1000)}-{i}-{name}"
            p.write_bytes(base64.b64decode(str(f.get("data", "")).partition(",")[2]))
            paths.append(str(p))
        return paths

    async def _save_images(self, data: dict) -> list[str]:
        paths = []
        folder = Path(self.store_path.parent) / "inbox" / "web"
        folder.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate(data.get("images") or []):
            head, _, b64 = str(url).partition(",")
            ext = "jpg" if "jpeg" in head else "png"
            p = folder / f"{int(time.time() * 1000)}-{i}.{ext}"
            p.write_bytes(base64.b64decode(b64))
            paths.append(str(p))
        return paths

    async def _api_schedules(self, request: web.Request) -> web.Response:
        b = self.bridge
        if request.method == "GET":
            return web.json_response({"tasks": b.schedules.rows(), "timezone": "Asia/Taipei", "error": b.schedules.load_error})
        if not self._same_origin(request): raise web.HTTPForbidden()
        data = await request.json()
        try:
            async with b.schedules.lock:
                action = data.get("action", "save")
                if action == "save":
                    task = b.create_schedule(data)
                    return web.json_response({"ok": True, "task": task})
                b.schedules.action(str(data.get("id") or ""), action)
            return web.json_response({"ok": True})
        except (ValueError, TypeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _read_send(self, request: web.Request) -> tuple[dict, list[str]]:
        """普通附件流式落盘；保留旧网页的 JSON 上传兼容。"""
        if not request.content_type.startswith("multipart/"):
            return await request.json(), []
        reader = await request.multipart()
        data, saved = None, []
        folder = self.store_path.parent / "inbox" / "web"
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
        from .schedules import parse_request
        try:
            plan = parse_request(text)
            if plan:
                if data.get("images") or data.get("files") or streamed_files:
                    raise ValueError("定时任务请先用文字创建；需要的文件放在工作目录中")
                from .app import inline_overrides, MODELS
                m, e = inline_overrides(text)
                model = "gpt" if m == "gpt" else next((k for k, v in MODELS.items() if v == m), "auto")
                plan.update(target=data.get("num"), model=data.get("model") or model, effort=data.get("effort") or e or "medium",
                            folder=data.get("folder") if data.get("folder") in self.bridge.cfg.folders else "")
                async with self.bridge.schedules.lock:
                    task = self.bridge.create_schedule(plan)
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
            busy = int(num) in b.runner.turns
            await b._message(Cmd("message", num=int(num), text=text), inc, routed=True)
            return web.json_response({"ok": True, "injected": busy})
        folder = str(data.get("folder") or "").strip()
        kind = None
        if folder == "scratch":
            folder = ""
        elif folder and folder not in b.cfg.folders:
            # 手输的路径或文件夹名：网页上没法像钉钉那样回“选编号”，所以找不到、找到几个都当场告诉你
            from . import folders as fd
            cands = fd.candidates(folder, b.cfg)
            if not cands:
                return web.json_response({"ok": False, "error": f"没找到文件夹“{folder}”。写完整路径（比如 ~/Projects/demo），或换一个文件夹名。"})
            if len(cands) > 1:
                return web.json_response({"ok": False, "error": f"“{folder}”找到好几个：" + "；".join(display(c) for c in cands[:4]) + "。请写完整路径。"})
            folder = str(cands[0])
        model, effort = self._model_effort(data)
        before = b.registry.next_num
        await b._new([folder] if folder else [], text, inc, note="（网页里新开）", kind=kind, model=model, effort=effort)
        return web.json_response({"ok": True, "num": before if b.registry.get(before) else None})

    @staticmethod
    def _model_effort(data: dict) -> tuple[str | None, str | None]:
        """网页上选的模型（opus / sonnet / haiku）和深度（low…max）；没选或不认得的返回 None。"""
        from .app import EFFORTS, MODELS
        m = str(data.get("model") or "")
        return ("gpt" if m == "gpt" else MODELS.get(m)), next((v for v in EFFORTS.values() if v and v == data.get("effort")), None)

    async def _api_settings(self, request: web.Request) -> web.Response:
        """改某个对话的模型和思考深度，下一轮起生效（正在做的这一轮不变）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        s = self.bridge.registry.get(int(request.match_info["num"]))
        if not s:
            raise web.HTTPNotFound()
        model, effort = self._model_effort(await request.json())
        if model == "gpt" or (s.engine == "codex" and model):
            model = None   # 一个对话从头到尾用同一家：中途不在 Claude 和 GPT 之间换
        s.model = model or s.model
        s.effort = effort or s.effort
        self.bridge.registry.save()
        self._threads.pop(s.num, None)
        return web.json_response({"ok": True})

    async def _api_presence(self, request: web.Request) -> web.Response:
        """网页每 15 秒报一次“我正开着 #N 看”；切走、锁屏时报不在。"""
        try:
            data = json.loads(await request.text() or "{}")
        except ValueError:
            data = {}
        num = int(data.get("num") or 0)
        if num and data.get("visible"):
            self.watch[num] = time.time()
        else:
            self.watch.pop(num, None) if num else self.watch.clear()
        return web.json_response({"ok": True})

    async def _api_fork(self, request: web.Request) -> web.Response:
        """分叉：从某条回复（或你某句话之前）另开一个对话，带着到那里为止的来龙去脉，原对话不动。
        {"to": 那条回复的记录编号}；空 = 从头另开（你第一句话之前，什么都不带）。文件不复制，两边改的是同一个文件夹。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        p = b.registry.get(int(request.match_info["num"]))
        if not p:
            raise web.HTTPNotFound()
        to = str((await request.json()).get("to") or "")
        if p.engine == "codex":
            try:
                cut = await self._codex_target(p, to) if to else ""
            except (ValueError, RuntimeError) as e:
                return web.json_response({"ok": False, "error": str(e)})
            records = self._records(p.num)
            ids = [r["id"] for r in records]
            kept = ids[:ids.index(to) + 1] if to else []
            now = time.time()
            s = Session(num=b.registry.reserve_num(), name=p.name[:8] + "·分叉", cwd=p.cwd,
                        add_dirs=list(p.add_dirs), engine="codex", model=p.model, effort=p.effort,
                        codex_id=p.codex_id if to else None, codex_records=kept, codex_cut_turn=cut,
                        fork_from=p.num, forking=bool(to), rewind_at=to,
                        rewind_note=f"从 #{p.num}「{p.name}」分叉", renamed=True, titled=True, seen=now, updated=now)
            b.registry.add(s)
            return web.json_response({"ok": True, "num": s.num, "name": s.name})
        if to:
            if not p.claude_id:
                return web.json_response({"ok": False, "error": "这个对话还没有记录"})
            items = thread.history(await asyncio.to_thread(thread.full_messages, p.claude_id, p.cwd), limit=100000)
            if not any(it.get("end") == to for it in items):
                return web.json_response({"ok": False, "error": "找不到那条回复了（可能在压缩之前）"})
        now = time.time()
        s = Session(num=b.registry.reserve_num(), name=(p.name[:8] + "·分叉"), cwd=p.cwd, add_dirs=list(p.add_dirs),
                    claude_id=p.claude_id if to else None, model=p.model, effort=p.effort, renamed=True, titled=True,
                    worked=p.worked, seen=now, updated=now, fork_from=p.num, forking=bool(to), rewind_at=to,
                    rewind_note=f"从 #{p.num}「{p.name}」分叉")
        b.registry.add(s)
        b.record("你（网页）", "分叉", num=s.num)
        return web.json_response({"ok": True, "num": s.num, "name": s.name})

    async def _api_rewind(self, request: web.Request) -> web.Response:
        async with self._rewind_lock:
            return await self._api_rewind_locked(request)

    async def _api_rewind_locked(self, request: web.Request) -> web.Response:
        """回到某条回复：之后的对话收起，下一句从那条接着说；可选把之后改过的文件也退回去。
        {"to": 那条回复的记录编号, "files": 要不要退文件} 或 {"cancel": true} 撤销（还没说下一句前）。
        正在做就先停下；排着的话一并撤掉（它们是接着后面说的）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        s = b.registry.get(int(request.match_info["num"]))
        if not s:
            raise web.HTTPNotFound()
        data = await request.json()
        if data.get("cancel"):
            if s.forking:
                return web.json_response({"ok": False, "error": "分叉出来的对话不能撤销，不要了就删掉它"})
            if s.engine == "codex":
                if s.codex_file_undo:
                    try:
                        self._file_restore_idle(s)
                        from .checkpoints import Checkpoints
                        await asyncio.to_thread(Checkpoints(self.store_path.parent / "checkpoints").undo, s.codex_file_undo)
                    except (OSError, ValueError) as e:
                        return web.json_response({"ok": False, "error": str(e)})
                    s.codex_file_undo = ""
                # 尚未开始下一轮，底层分支还没切换；恢复被收起的记录。
                s.codex_hidden = [x for x in s.codex_hidden if x not in getattr(s, "codex_pending_hidden", [])]
                s.codex_pending_hidden = []
                s.codex_cut_turn = ""
            s.rewind_at = s.rewind_note = ""
            self._threads.pop(s.num, None)
            b.registry.save()
            return web.json_response({"ok": True})
        if s.engine == "codex":
            return await self._rewind_codex(s, data)
        if not s.claude_id:
            return web.json_response({"ok": False, "error": "这个对话还没有记录"})
        to = str(data.get("to") or "")
        while b.runner.unqueue_last(s.num):
            pass
        if s.num in b.runner.turns:
            await b.runner.stop(s.num)
            for _ in range(120):
                if s.num not in b.runner.turns:
                    break
                await asyncio.sleep(0.5)
        b.outgoing.pop(s.num, None)
        items = thread.history(await asyncio.to_thread(thread.full_messages, s.claude_id, s.cwd), limit=100000)
        k = next((i for i, it in enumerate(items) if it.get("end") == to), None)
        if k is None:
            return web.json_response({"ok": False, "error": "找不到那条回复了（可能在压缩之前）"})
        later = [it for it in items[k + 1:] if it["who"] == "you"]
        if not later:
            return web.json_response({"ok": False, "error": "这已经是最新的一条了"})
        note = f"已回到这里 · 收起 {len(later)} 轮"
        if data.get("files"):
            why = await b.runner.rewind_files(s, later[0]["uuid"])
            note += " · " + (why or "文件已退回")
        s.rewind_at, s.rewind_note = to, note
        kept = items[k]["text"]
        from .app import asks_user, awaiting_kind
        s.awaiting = asks_user(kept)
        s.awaiting_kind = awaiting_kind(kept) if s.awaiting else ""
        s.limit_until = 0.0
        b.registry.save()
        self._threads.pop(s.num, None)
        return web.json_response({"ok": True, "note": note, "text": later[0]["text"]})

    def _file_restore_idle(self, s):
        roots = [Path(p).expanduser().resolve() for p in [s.cwd, *s.add_dirs]]
        for other in self.bridge.registry.sessions.values():
            if not self.bridge.runner.busy(other.num):
                continue
            theirs = [Path(p).expanduser().resolve() for p in [other.cwd, *other.add_dirs]]
            if any(a.is_relative_to(b) or b.is_relative_to(a) for a in roots for b in theirs):
                raise ValueError(f"#{other.num} 还在使用这个文件夹，请等它停下后再恢复文件")

    async def _rewind_codex(self, s, data: dict) -> web.Response:
        b = self.bridge
        to = str(data.get("to") or "")
        try:
            cut = await self._codex_target(s, to)
        except (ValueError, RuntimeError) as e:
            return web.json_response({"ok": False, "error": str(e)})
        while b.runner.unqueue_last(s.num):
            pass
        if b.runner.busy(s.num):
            await b.runner.stop(s.num)
            for _ in range(120):
                if not b.runner.busy(s.num):
                    break
                await asyncio.sleep(.5)
            if b.runner.busy(s.num):
                return web.json_response({"ok": False, "error": "对话还没有停下，请稍后再回退"})
        items = self._codex_history(s.num)
        k = next((i for i, it in enumerate(items) if it.get("end") == to), None)
        if k is None:
            return web.json_response({"ok": False, "error": "找不到那条回复，原对话未改动"})
        later = [it for it in items[k + 1:] if it["who"] == "you"]
        if not later:
            return web.json_response({"ok": False, "error": "这已经是最新的一条了"})
        ids = [r["id"] for r in self._records(s.num)]
        removed = ids[ids.index(to) + 1:]
        # 停止后报告可能还在翻译、存档，提前收起它，防止晚到的记录重新出现。
        last = b.runner.last.get(s.num)
        if last:
            tid = f"{s.num}-{int(last.started)}"
            if tid not in ids and tid not in s.codex_hidden:
                removed.append(tid)
        if data.get("files"):
            if s.codex_file_undo:
                return web.json_response({"ok": False, "error": "请先撤销当前文件回退，再选择其他回退位置"})
            try:
                self._file_restore_idle(s)
                from .checkpoints import Checkpoints
                records = {r["id"]: r for r in self._records(s.num)}
                if last:
                    records.setdefault(f"{s.num}-{int(last.started)}", {
                        "checkpoint_before": last.checkpoint_before, "checkpoint_after": last.checkpoint_after})
                pairs = [(records.get(tid, {}).get("checkpoint_before", ""), records.get(tid, {}).get("checkpoint_after", "")) for tid in removed]
                if not pairs:
                    raise ValueError("没有可用的文件快照，文件保持现状")
                store = Checkpoints(self.store_path.parent / "checkpoints")
                plan = await asyncio.to_thread(store.plan, pairs)
                s.codex_file_undo = await asyncio.to_thread(store.apply, plan)
            except (OSError, ValueError) as e:
                return web.json_response({"ok": False, "error": str(e)})
        s.codex_pending_hidden.extend(x for x in removed if x not in s.codex_pending_hidden)
        s.codex_hidden.extend(x for x in removed if x not in s.codex_hidden)
        s.codex_cut_turn = cut
        s.rewind_at, s.rewind_note = to, f"已回到这里 · 收起 {len(later)} 轮" + (" · 文件已恢复" if data.get("files") else "")
        s.awaiting = s.awaiting_kind = ""
        s.limit_until = 0.0
        b.outgoing.pop(s.num, None)
        b.registry.save()
        self._threads.pop(s.num, None)
        return web.json_response({"ok": True, "note": s.rewind_note, "text": later[0]["text"]})

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

    def _sig_sessions(self) -> str:
        """对话列表的“指纹”：谁在做、做到第几步、等不等你、更新时间；变了就叫网页刷新列表。"""
        b = self.bridge
        out = []
        for s in b.registry.sessions.values():
            t = b.runner.turns.get(s.num)
            out.append((s.num, s.updated, s.awaiting, s.seen > s.updated, bool(t), len(t.steps) if t else 0,
                        bool(t and t.confirm), b.runner.busy(s.num), s.hidden, s.pinned, s.name, s.limit_until,
                        s.rewind_at, s.auto_resume))
        return json.dumps(out, ensure_ascii=False, default=str)

    def _cc_mtime(self, s) -> int:
        """Claude 记录文件的修改时间：Claude Code 那边（终端 / IDE）接着聊也会写这份记录，变了就该刷新。"""
        if not s or not s.claude_id:
            return 0
        p = thread.transcript_path(s.claude_id)
        try:
            return p.stat().st_mtime if p else 0
        except OSError:
            return 0

    def _sig_thread(self, num: int) -> str:
        b = self.bridge
        s, t = b.registry.get(num), b.runner.turns.get(num)
        recs = len(list(self.turns_dir.glob(f"{num}-*.json"))) if s else 0
        cc_mtime = self._cc_mtime(s)
        return json.dumps([s.updated if s else 0, len(t.timeline) if t else -1, bool(t and t.confirm),
                           len(b.outgoing.get(num, [])), recs, s.rewind_at if s else "", len(b.runner.queued(num)),
                           s.limit_until if s else 0, s.auto_resume if s else True, cc_mtime])

    async def _api_events(self, request: web.Request) -> web.StreamResponse:
        """实时推送（Server-Sent Events）：列表变了发 s，打开的对话有新步骤 / 做完了发 t，正在写的回复发 p（带全文）。
        网页收到就当场更新，不用每几秒问一次。每 15 秒发个空行保活（反向代理 120 秒没动静会断）。"""
        num = int(request.query.get("num") or 0)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        last_s = last_t = last_p = None
        beat = time.time()
        try:
            while True:
                sig_s = self._sig_sessions()
                if sig_s != last_s:
                    if last_s is not None:
                        await resp.write(b"event: s\ndata: 1\n\n")
                    last_s = sig_s
                if num:
                    sig_t = self._sig_thread(num)
                    if sig_t != last_t:
                        if last_t is not None:
                            await resp.write(b"event: t\ndata: 1\n\n")
                        last_t = sig_t
                    t = self.bridge.runner.turns.get(num)
                    part = t.partial if t else ""
                    if part != last_p:
                        await resp.write(b"event: p\ndata: " + json.dumps({"text": part}, ensure_ascii=False).encode() + b"\n\n")
                        last_p = part
                if time.time() - beat > 15:
                    await resp.write(b": ping\n\n")
                    beat = time.time()
                await asyncio.sleep(0.4)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _api_uimg(self, request: web.Request) -> web.Response:
        """对话里你发过的图。"""
        s = self.bridge.registry.get(int(request.match_info["num"]))
        ref = f"{request.match_info['uuid']}/{request.match_info['k']}"
        if not s or not s.claude_id or ref in self._uimg_miss:
            raise web.HTTPNotFound()
        got = self._uimg.get(ref) or await asyncio.to_thread(thread.user_image, s.claude_id, s.cwd, ref)
        if not got:
            self._uimg_miss.add(ref)
            raise web.HTTPNotFound()
        self._uimg[ref] = got
        self._uimg = dict(list(self._uimg.items())[-60:])
        return web.Response(body=got[0], content_type=got[1], headers={"Cache-Control": "private, max-age=86400"})

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

    async def _api_stop(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        stopped = await self.bridge.runner.stop(int(request.match_info["num"]))
        return web.json_response({"ok": True, "stopped": stopped})

    # ---------- 启动 ----------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.path in (f"{PREFIX}/login", f"{PREFIX}/icon.png", f"{PREFIX}/icon-512.png", f"{PREFIX}/manifest.json", f"{PREFIX}/sw.js",
                            f"{PREFIX}/health"):
            return await handler(request)
        if not self._logged_in(request):
            return self._denied(request)
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard], client_max_size=0)
        app.cleanup_ctx.append(self._speech_context)
        app.router.add_get(f"{PREFIX}/login", self._login_get)
        app.router.add_post(f"{PREFIX}/login", self._login_post)
        app.router.add_get("/", self._to_index)
        app.router.add_get(f"{PREFIX}/", self._app_page)
        app.router.add_get(f"{PREFIX}/history", self._index)
        app.router.add_get(f"{PREFIX}/icon.png", self._icon)
        app.router.add_get(f"{PREFIX}/icon-512.png", self._icon)
        app.router.add_get(f"{PREFIX}/manifest.json", self._manifest)
        app.router.add_get(f"{PREFIX}/sw.js", self._sw)
        app.router.add_get(f"{PREFIX}/health", self._health)
        app.router.add_get(f"{PREFIX}/api/push/key", self._api_push_key)
        app.router.add_post(f"{PREFIX}/api/push", self._api_push)
        app.router.add_post(f"{PREFIX}/api/logout", self._api_logout)
        app.router.add_get(f"{PREFIX}/api/devices", self._api_devices)
        app.router.add_post(f"{PREFIX}/api/devices/logout", self._api_device_logout)
        app.router.add_get(f"{PREFIX}/api/sessions", self._api_sessions)
        app.router.add_get(f"{PREFIX}/api/events", self._api_events)
        app.router.add_get(f"{PREFIX}/api/uimg/{{num:\\d+}}/{{uuid}}/{{k:\\d+}}", self._api_uimg)
        app.router.add_get(f"{PREFIX}/api/thread/{{num:\\d+}}", self._api_thread)
        app.router.add_post(f"{PREFIX}/api/manage/{{num:\\d+}}", self._api_manage)
        app.router.add_post(f"{PREFIX}/api/speech/{{num:\\d+}}", self._api_speech)
        app.router.add_post(f"{PREFIX}/api/speech/release", self._release_speech)
        app.router.add_get(f"{PREFIX}/api/audio/{{key}}", self._api_audio)
        app.router.add_post(f"{PREFIX}/api/audio/{{key}}", self._api_audio)
        app.router.add_get(f"{PREFIX}/audio/{{key}}.mp3", self._audio)
        app.router.add_get(f"{PREFIX}/api/search", self._api_search)
        app.router.add_post(f"{PREFIX}/api/send", self._api_send)
        app.router.add_get(f"{PREFIX}/api/schedules", self._api_schedules)
        app.router.add_post(f"{PREFIX}/api/schedules", self._api_schedules)
        app.router.add_post(f"{PREFIX}/api/settings/{{num}}", self._api_settings)
        app.router.add_post(f"{PREFIX}/api/compact/{{num:\\d+}}", self._api_compact)
        app.router.add_post(f"{PREFIX}/api/confirm/{{num:\\d+}}", self._api_confirm)
        app.router.add_post(f"{PREFIX}/api/stop/{{num:\\d+}}", self._api_stop)
        app.router.add_post(f"{PREFIX}/api/rewind/{{num:\\d+}}", self._api_rewind)
        app.router.add_post(f"{PREFIX}/api/fork/{{num:\\d+}}", self._api_fork)
        app.router.add_get(f"{PREFIX}/api/browse", self._api_browse)
        app.router.add_get(f"{PREFIX}/api/cc-sessions", self._api_cc_sessions)
        app.router.add_post(f"{PREFIX}/api/resume-cc", self._api_resume_cc)
        app.router.add_post(f"{PREFIX}/api/unqueue/{{num:\\d+}}", self._api_unqueue)
        app.router.add_post(f"{PREFIX}/api/presence", self._api_presence)
        app.router.add_get(PREFIX, self._to_index)
        app.router.add_get(f"{PREFIX}/t/{{tid}}", self._turn_page)
        app.router.add_get(f"{PREFIX}/t/{{tid}}/img/{{i:\\d+}}", self._turn_image)
        app.router.add_get(f"{PREFIX}/t/{{tid}}/file/{{i:\\d+}}", self._turn_file)
        # 网页里下文件走 /_x 外面的地址：iPhone、iPad 从主屏幕打开的远窗，点范围外的链接会弹出带“完成”的浏览页，
        # 看完能回来；范围内的链接直接在远窗窗口里打开文件，没有返回键（10-27 遇到）
        app.router.add_get("/dl/{tid}/{i:\\d+}", self._turn_file)
        app.router.add_get("/dl/{tid}/{i:\\d+}/pages", self._pdf_pages)
        app.router.add_get("/dl/{tid}/{i:\\d+}/p/{name}", self._pdf_page)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        log.info("远窗网页在 127.0.0.1:%d", self.port)
