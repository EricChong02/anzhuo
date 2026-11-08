"""登录、设备、退出。"""
from __future__ import annotations

import hashlib
import html
import logging
import secrets
import time

from aiohttp import web

from ..brand import LOGIN_CSS, MARK
from .common import COOKIE, LOCK_ALL, LOCK_IP, SESSION_DAYS, check_password

log = logging.getLogger(__name__)


class AuthMixin:
    def _logged_in(self, request: web.Request) -> bool:
        sid = request.cookies.get(COOKIE, "")
        ok = bool(sid) and self.store["sessions"].get(sid, 0) > time.time()
        acct = self._acct_of(sid) if ok else ""
        if ok and (acct not in self.accounts or acct in self.store.get("disabled", [])):
            ok = False   # 账号被停用或删掉了：这台设备进不来
        if ok:
            request["acct"], request["owner"] = acct, self.owner_key(acct)
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

    def _safe_next(self, target: str) -> str:
        return target if target.startswith("/") and not target.startswith("//") else f"{self.px}/"

    def _login_form(self, nxt: str, msg: str = "", status: int = 200) -> web.Response:
        note = f'<p class="err">{html.escape(msg)}</p>' if msg else ""
        body = LOGIN_CSS + f"""
<form class="login" method="post" action="{self.px}/login">
{MARK}<h1>远窗</h1><p class="sub">登录一次，这台设备 30 天免登录</p>{note}
<input type="hidden" name="next" value="{html.escape(nxt)}">
<input name="username" placeholder="账号" autocomplete="username" autocapitalize="off" required>
<input name="password" type="password" placeholder="密码" autocomplete="current-password" required>
<button>登录</button></form>"""
        return self._page("登录 · 远窗", body, status)

    async def _login_get(self, request: web.Request) -> web.Response:
        nxt = self._safe_next(request.query.get("next", f"{self.px}/"))
        if self._logged_in(request):
            raise web.HTTPFound(nxt)
        return self._login_form(nxt)

    async def _login_post(self, request: web.Request) -> web.Response:
        form = await request.post()
        nxt = self._safe_next(str(form.get("next", f"{self.px}/")))
        ip = self._client_ip(request)
        if wait := self._locked(ip):
            return self._login_form(nxt, f"错太多次了，{max(1, wait // 60)} 分钟后再试。", 429)
        name = str(form.get("username", "")).strip()
        acct = self.accounts.get(name) if name not in self.store.get("disabled", []) else None
        ok = bool(acct and acct.get("hash") and check_password(str(form.get("password", "")), acct["hash"]))
        if not ok:
            now = time.time()
            self.fails.setdefault(ip, []).append(now)
            self.all_fails.append(now)
            log.warning("网页登录失败：%s", ip)
            return self._login_form(nxt, "账号或密码不对。", 401)
        self.fails.pop(ip, None)
        sid = secrets.token_urlsafe(32)
        self.store["sessions"][sid] = time.time() + SESSION_DAYS * 86400
        self.store.setdefault("devices", {})[sid] = {"created": time.time(), "seen": time.time(), "ip": ip, "account": name,
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
        return web.json_response({"devices": self._device_rows(me, request["acct"])})

    def _device_rows(self, me: str, acct: str | None) -> list[dict]:
        """登录着的设备（acct=None 列所有账号的，管理页用）。"""
        now = time.time()
        rows = []
        for sid, exp in self.store["sessions"].items():
            if exp <= now or (acct is not None and self._acct_of(sid) != acct):
                continue
            d = self.store.get("devices", {}).get(sid, {})
            ip = d.get("ip", "")
            rows.append({"id": self._device_id(sid), "name": self._device_name(d.get("ua", "")), "me": sid == me,
                         "seen": d.get("seen") or d.get("created") or 0, "created": d.get("created") or 0,
                         "ip": ".".join(ip.split(".")[:2] + ["*", "*"]) if ip.count(".") == 3 else ip,
                         "push": bool(d.get("endpoint")), "account": self._acct_of(sid)})
        rows.sort(key=lambda r: (not r["me"], -r["seen"]))
        return rows

    def _drop_device(self, sid: str) -> None:
        dev = self.store.get("devices", {}).pop(sid, {})
        self.store["sessions"].pop(sid, None)
        if dev.get("endpoint"):
            self.push.remove(dev["endpoint"])

    async def _api_device_logout(self, request: web.Request) -> web.Response:
        """退出某一台（不能用来退出自己这台，那个走“退出登录”）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        want = str((await request.json()).get("id") or "")
        me = request.cookies.get(COOKIE, "")
        for sid in list(self.store["sessions"]):
            if self._device_id(sid) == want and sid != me and self._acct_of(sid) == request["acct"]:
                self._drop_device(sid)
                self._save()
                return web.json_response({"ok": True})
        return web.json_response({"ok": False}, status=404)

    async def _api_logout(self, request: web.Request) -> web.Response:
        """退出登录：只退这台设备，或 all=true 时所有设备都退（手机丢了用）；这台设备的手机通知也一并停掉。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        data = await request.json()
        if data.get("all"):   # 只退这个账号的所有设备
            for sid in [k for k in self.store["sessions"] if self._acct_of(k) == request["acct"]]:
                self._drop_device(sid)
        else:
            self.store["sessions"].pop(request.cookies.get(COOKIE, ""), None)
            if data.get("endpoint"):
                self.push.remove(str(data["endpoint"]))
        self._save()
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp
