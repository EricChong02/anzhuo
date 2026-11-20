"""管理页与设置页（只有管理员）。"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

from .. import usage
from ..folders import display
from .common import COOKIE, hash_password

log = logging.getLogger(__name__)


class AdminMixin:
    # ---------- 管理页（只有 admin）：账号与登录、额度消耗 ----------
    async def _admin_page(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{self.px}/#admin")   # 管理是主界面里的一个视图

    async def _api_admin(self, request: web.Request) -> web.Response:
        b = self.bridge
        if not getattr(b.runner, "limits_gpt", None):   # GPT 额度平时只在有 GPT 对话时才读；管理页总要看得到
            try:
                await b.runner.refresh_gpt_usage()
            except Exception:  # noqa: BLE001
                log.debug("读 GPT 额度失败", exc_info=True)
        now = time.time()
        current_week = request.query.get("period") == "week"
        days = max(7, min(90, int(request.query.get("days") or 30)))
        week = b.runner.limits.get("seven_day")
        reset = usage.reset_epoch(week[1], now=now) if week else None
        cycle_start = usage.week_start(reset, now=now)
        names = {n: s.name for n, s in b.registry.sessions.items()}
        rows = usage.load(self.usage_path, (cycle_start if cycle_start is not None else now) if current_week else now - (days + 1) * 86400)
        if current_week and cycle_start is None:
            rows = []
        me = request.cookies.get(COOKIE, "")
        accts = []
        for name, a in self.accounts.items():
            accts.append({"name": name, "role": a.get("role"), "root": display(Path(a["root"]).expanduser()) if a.get("root") else "",
                          "disabled": name in self.store.get("disabled", []), "me": name == request["acct"],
                          "owner": self.owner_key(name), "quota": self.quota(name),
                          "devices": [d for d in self._device_rows(me, None) if d["account"] == name]})
        start = cycle_start if cycle_start is not None else now - usage.WEEK
        rate = usage.rate(usage.load(self.usage_path, start - 3 * usage.WEEK), start - 3 * usage.WEEK,
                          getattr(b, "spend", None)) or self.store.get("rate2")
        return web.json_response({"accounts": accts, "usage": usage.summary(rows, names, days, now=now, rate=rate,
                                                                            since=(cycle_start if cycle_start is not None else now) if current_week else None,
                                                                            groups=usage.relay_groups(b.registry.sessions)),
                                  "limits": self._limits(b.runner.limits),
                                  "limits_gpt": self._limits(getattr(b.runner, "limits_gpt", {})),
                                  "period": "week" if current_week else "days", "week_available": cycle_start is not None,
                                  "period_start": cycle_start if current_week else None, "rate": rate})

    # ---------- 设置（只有 admin）：读写 config.toml 里常改的项，见 prefs.py ----------
    async def _api_prefs(self, request: web.Request) -> web.Response:
        from .. import prefs
        if request.method == "GET":
            return web.json_response(prefs.payload(self.bridge, self.config_path))
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        return web.json_response(prefs.update(self.bridge, await request.json(), self.config_path))

    async def _settings_js(self, request: web.Request) -> web.StreamResponse:
        from ..webui import settings_js
        return web.Response(text=settings_js(self.web_prefix), content_type="application/javascript", headers={"Cache-Control": "no-store"})

    async def _api_admin_act(self, request: web.Request) -> web.Response:
        """改密码、停用 / 启用账号、调额度比例、退出某台设备或某个账号的所有设备。admin 自己不能停用自己。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        d = await request.json()
        act, name = d.get("action"), str(d.get("account") or "")
        if name not in self.accounts:
            return web.json_response({"ok": False, "error": "没有这个账号"}, status=404)
        if act == "password":
            pw = str(d.get("value") or "")
            if len(pw) < 6:
                return web.json_response({"ok": False, "error": "密码至少 6 位"})
            digest = hash_password(pw)
            if self.save_hash:
                await asyncio.to_thread(self.save_hash, name, digest)
            self.accounts[name]["hash"] = digest
        elif act in ("disable", "enable"):
            if self.role(name) == "admin":
                return web.json_response({"ok": False, "error": "管理员账号不能停用"})
            dis = set(self.store.get("disabled", []))
            dis.add(name) if act == "disable" else dis.discard(name)
            self.store["disabled"] = sorted(dis)
        elif act == "share":
            if self.role(name) == "admin":
                return web.json_response({"ok": False, "error": "管理员不限额度"})
            try:
                share = float(d.get("value"))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "比例不对"})
            if not 0 < share <= 1:
                return web.json_response({"ok": False, "error": "比例要在 1% 到 100% 之间"})
            self.store.setdefault("shares", {})[name] = share
        elif act == "logout":
            want = str(d.get("id") or "")
            me = request.cookies.get(COOKIE, "")
            for sid in list(self.store["sessions"]):
                if self._device_id(sid) == want and sid != me and self._acct_of(sid) == name:
                    self._drop_device(sid)
        elif act == "logout_all":
            me = request.cookies.get(COOKIE, "")
            for sid in [k for k in self.store["sessions"] if self._acct_of(k) == name and k != me]:
                self._drop_device(sid)
        else:
            raise web.HTTPBadRequest()
        self._save()
        return web.json_response({"ok": True})
