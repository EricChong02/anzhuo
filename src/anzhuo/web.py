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

from . import thread, translate, usage, task_groups
from .brand import LOGIN_CSS, MARK
from .push import Push
from .speech import Speech
from .sessions import COMPACT, Session
from .cards import parse_decision
from .acp import ENGINES as ACP_ENGINES, is_acp, available as acp_available
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
PUBLIC = (f"{PREFIX}/login", f"{PREFIX}/icon.png", f"{PREFIX}/icon-512.png", f"{PREFIX}/manifest.json", f"{PREFIX}/sw.js",
          f"{PREFIX}/health")
# 受限账号（role = user）能用的页面和接口；别的（历史记录、试课、定时、浏览文件夹、接电脑端对话、管理页）一律不给
USER_ROUTES = {f"{PREFIX}/", PREFIX, "/", f"{PREFIX}/api/push/key", f"{PREFIX}/api/push", f"{PREFIX}/api/logout",
               f"{PREFIX}/api/devices", f"{PREFIX}/api/devices/logout", f"{PREFIX}/api/sessions", f"{PREFIX}/api/events",
               f"{PREFIX}/api/uimg/{{num}}/{{uuid}}/{{k}}", f"{PREFIX}/api/thread/{{num}}", f"{PREFIX}/api/manage/{{num}}",
               f"{PREFIX}/api/speech/{{num}}", f"{PREFIX}/api/speech/release", f"{PREFIX}/api/audio/{{key}}",
               f"{PREFIX}/audio/{{key}}.mp3", f"{PREFIX}/api/search", f"{PREFIX}/api/send", f"{PREFIX}/api/settings/{{num}}",
               f"{PREFIX}/api/compact/{{num}}", f"{PREFIX}/api/relay/{{num}}", f"{PREFIX}/api/confirm/{{num}}", f"{PREFIX}/api/stop/{{num}}",
               f"{PREFIX}/api/rewind/{{num}}", f"{PREFIX}/api/fork/{{num}}", f"{PREFIX}/api/unqueue/{{num}}",
               f"{PREFIX}/api/presence", f"{PREFIX}/t/{{tid}}", f"{PREFIX}/t/{{tid}}/img/{{i}}", f"{PREFIX}/t/{{tid}}/file/{{i}}",
               "/dl/{tid}/{i}", "/dl/{tid}/{i}/pages", "/dl/{tid}/{i}/p/{name}"}

RELAY_LONG = 200_000   # 上下文到这么长就提示接力：每说一句都要把它重读一遍
RELAY_COLD = 80_000    # 隔了一个多小时再回来、上下文又有这么长：缓存过期，接着说要整段重写一遍
CACHE_TTL = 3300       # Claude 的提示缓存约 1 小时过期，留 5 分钟余量


def relay_hint(s, busy: bool, relaying: bool, now: float | None = None) -> dict | None:
    """该不该提示“接力到新对话”。返回 {kind: long/cold, tokens, hours, key}；key 用来记你点过“不用”。"""
    if relaying:
        return {"kind": "running"}
    tk = s.context_tokens or 0
    if busy or s.relay_to or not (s.claude_id or s.codex_id) or tk < RELAY_COLD:
        return None
    idle = (now or time.time()) - s.updated
    if s.engine == "claude" and idle > CACHE_TTL:
        return {"kind": "cold", "tokens": tk, "hours": round(idle / 3600, 1), "key": f"cold{int(s.updated)}"}
    if tk >= RELAY_LONG:
        return {"kind": "long", "tokens": tk, "key": f"long{tk // 100_000}"}
    return None


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
        ["gpt", display_model(configured_model()) or "GPT"]] + [[k, v["name"]] for k, v in ACP_ENGINES.items() if acp_available(k)]

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


_RELAY_MARK = "用户这次说：\n\n"


def _said_has(recorded: str, sent: str) -> bool:
    """记录里的这句是不是你发的那句。接力对话的第一句，记录里是“交接说明 + 用户这次说：+ 原话”。"""
    head = sent.strip()[:40]
    if recorded.startswith(head):
        return True
    return recorded.startswith("（这是接力的新对话") and _RELAY_MARK in recorded and recorded.split(_RELAY_MARK, 1)[1].startswith(head)


class Web:
    async def _api_task_groups(self, request):
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        owner = request.get("owner", "admin")
        if owner != "admin":
            raise web.HTTPForbidden()
        if not self.bridge.cfg.task_grouping:
            return web.json_response({"ok": True, "count": 0})
        data = await request.json()
        if owner in self._group_busy:
            return web.json_response({"ok": False, "busy": True})
        if not data.get("force") and time.time() - self._group_attempt.get(owner, 0) < 300:
            return web.json_response({"ok": False, "retry_later": True})
        sessions = [s for s in self.bridge.registry.sessions.values()
                    if s.owner == owner and not s.hidden and self._relay_latest(s).num == s.num]
        targets = sorted([s for s in sessions if not s.task_group_manual and (data.get("force") or not s.task_group)],
                         key=lambda s: -s.updated)[:80]
        if not targets:
            return web.json_response({"ok": True, "count": 0})
        self._group_busy.add(owner)
        self._group_attempt[owner] = time.time()
        try:
            items = [{"num": s.num, "title": self._relay_chain(s)[0].name[:60],
                      "folder": Path(s.cwd).name[:80], "summary": s.last_summary[:400]}
                     for s in targets]
            result, model = await task_groups.classify(items, sorted({s.task_group for s in sessions if s.task_group}),
                                                       cache=None if data.get("force") else self.store_path.with_name("task_groups.json"))
            count = 0
            for s in targets:
                # 分类期间用户可能改了分组、删除或完成接力；以当前现场为准。
                if s.task_group_manual or s.hidden or s.owner != owner:
                    continue
                latest = self._relay_latest(s)
                if latest.task_group_manual:
                    continue
                for member in self._relay_chain(latest):
                    member.task_group = result[s.num]
                count += 1
            self.bridge.registry.save()
            return web.json_response({"ok": True, "count": count, "model": model})
        except Exception:
            log.warning("任务归组失败，保留原分组")
            return web.json_response({"ok": False, "error": "暂时没整理成功，原有分组已保留"})
        finally:
            self._group_busy.discard(owner)


    def __init__(self, base: str, port: int, data_dir: Path,
                 user: str = "", pw_hash: str = "", watch_host: str = "", accounts: dict[str, dict] | None = None,
                 save_hash=None, web_prefix: str = ""):
        # 子路径挂载：被别的面板反代透传时，整站挂在 {web_prefix}/... 下（见 config.example.toml 的 web_prefix）。空 = 根路径，现状不变
        prefix = (web_prefix or "").strip()
        if prefix and not prefix.startswith("/"):
            prefix = "/" + prefix
        self.web_prefix = prefix.rstrip("/")
        self.px = self.web_prefix + PREFIX   # /_x 命名空间的实际地址，比如 /yuanchuang/_x
        # 账号：名字 → {role: admin / user, root: user 的工作文件夹, hash: 密码指纹}。老写法（user + pw_hash）当作唯一的 admin
        self.accounts = dict(accounts) if accounts else ({user: {"role": "admin", "root": "", "hash": pw_hash}} if user else {})
        self.save_hash = save_hash  # 管理页改密码时存指纹（默认存钥匙串）
        self.usage_path = data_dir / "usage.jsonl"
        self.fails: dict[str, list[float]] = {}
        self.all_fails: list[float] = []
        self.base = base.rstrip("/")
        self.port = port
        self.turns_dir = data_dir / "turns"
        self.store_path = data_dir / "web.json"
        self.push = Push(data_dir / "push.json", self.base or "https://localhost", mirror=watch_host)
        self.store = self._load()
        # 鉴权用的路径集合按前缀重算：PUBLIC / USER_ROUTES 是按 /_x 写的模块常量，挂了子路径就整体平移
        # （/dl 不在 /_x 命名空间里，路由挂到 {web_prefix}/dl，鉴权集合要跟着搬，不然非管理员下文件会被 403）
        def _shift(p: str) -> str:
            if p.startswith(PREFIX):
                return self.px + p[len(PREFIX):]
            if self.web_prefix and p.startswith("/dl"):
                return self.web_prefix + p
            return p
        self.public = tuple(_shift(p) for p in PUBLIC)
        self.user_routes = {_shift(p) for p in USER_ROUTES}
        if self.web_prefix:
            self.user_routes |= {f"{self.web_prefix}/", self.web_prefix}   # 挂载点的入口重定向，谁都能走到主界面
        self._rewind_lock = asyncio.Lock()
        self.speech = Speech(data_dir / "speech")
        self.bridge = None  # 小E 本体（app.Bridge），网页对话框靠它看对话、发话
        self._group_busy: set[str] = set()
        self._group_attempt: dict[str, float] = {}
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
        return {"sessions": d.get("sessions", {}), "devices": d.get("devices", {}), "disabled": d.get("disabled", []),
                "shares": d.get("shares", {}), "rate2": d.get("rate2")}

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

    def _acct_of(self, sid: str) -> str:
        """这台设备登的是哪个账号（这个功能之前登录的都算 admin）。"""
        return self.store.get("devices", {}).get(sid, {}).get("account") or self._admin()

    def _admin(self) -> str:
        return next((n for n, a in self.accounts.items() if a.get("role") == "admin"), "admin")

    # 老写法：只有一个账号（admin）时直接读写它的名字和密码指纹
    @property
    def user(self) -> str:
        return self._admin()

    @user.setter
    def user(self, name: str) -> None:
        old = self.accounts.pop(self._admin(), {"role": "admin", "root": "", "hash": ""})
        self.accounts[name] = old

    @property
    def pw_hash(self) -> str:
        return (self.accounts.get(self._admin()) or {}).get("hash", "")

    @pw_hash.setter
    def pw_hash(self, digest: str) -> None:
        self.accounts.setdefault(self._admin(), {"role": "admin", "root": ""})["hash"] = digest

    def role(self, acct: str) -> str:
        return (self.accounts.get(acct) or {}).get("role", "user")

    def root_of(self, acct: str) -> str:
        return (self.accounts.get(acct) or {}).get("root", "") if self.role(acct) != "admin" else ""

    def owner_key(self, acct: str) -> str:
        """对话登记里记的归属：admin 账号的一律记 "admin"（钉钉开的也是），别的账号记账号名。"""
        return "admin" if self.role(acct) == "admin" else acct

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

    async def notify(self, owner: str, title: str, body: str, num: int | None = None) -> int:
        """手机通知只发给这个对话所属账号登录的设备。没对上设备的老订阅算 admin 的。"""
        mine, others = set(), set()
        for sid, d in self.store.get("devices", {}).items():
            if d.get("endpoint"):
                (mine if self.owner_key(self._acct_of(sid)) == owner else others).add(d["endpoint"])
        targets = [x["endpoint"] for x in self.push.subs
                   if x["endpoint"] in mine or (owner == "admin" and x["endpoint"] not in others)]
        return await self.push.send(title, body, num, only=set(targets))

    def quota(self, acct: str) -> dict | None:
        """受限账号本周额度用了多少（admin 不限，返回 None）。顺手把推出来的换算比例存下，数据少的时候接着用。"""
        if self.role(acct) == "admin":
            return None
        limits = getattr(getattr(self.bridge, "runner", None), "limits", {}) or {}
        week = limits.get("seven_day")
        share = float(self.store.get("shares", {}).get(acct, (self.accounts.get(acct) or {}).get("share", usage.DEFAULT_SHARE)))
        q = usage.quota(usage.load(self.usage_path, time.time() - 29 * 86400), self.owner_key(acct), share,
                        usage.reset_epoch(week[1]) if week else None, self.store.get("rate2"),
                        spend=getattr(self.bridge, "spend", None))
        if q["rate"] and q["rate"] != self.store.get("rate2"):   # rate2：2026-11-04 之前存的 rate 是按累计值算错的，不用
            self.store["rate2"] = q["rate"]
            self._save()
        q["reset"] = time.strftime("%-m月%-d日 %H:%M", time.localtime(q["reset_at"])) if q["reset_at"] else ""
        return q

    def over_quota(self, acct: str) -> str:
        """额度用完了返回一句说明，没用完返回空。"""
        q = self.quota(acct)
        if not q or q["pct"] < 100:
            return ""
        return "本周额度已用完" + (f"，{q['reset']} 重置后再继续。" if q["reset"] else "，重置后再继续。")

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
    def _page(self, title: str, body: str, status: int = 200) -> web.Response:
        page = PAGE.format(title=html.escape(title), body=body)
        if self.web_prefix:   # PAGE 是按 /_x 写的模板，挂了子路径就把里面的引用平移
            page = page.replace('href="/_x/"', f'href="{self.px}/"')
        return web.Response(text=page, status=status,
                            content_type="text/html", headers={"Cache-Control": "no-store",
                                                               "Referrer-Policy": "no-referrer"})

    def _denied(self, request: web.Request) -> web.Response:
        if request.method == "GET" and "/api/" not in request.path:
            raise web.HTTPFound(f"{self.px}/login?next={quote(request.rel_url.path_qs, safe='')}")
        return web.json_response({"detail": "需要先登录远窗"}, status=401)

    async def _app_page(self, request: web.Request) -> web.Response:
        from .webui import app_html
        return web.Response(text=app_html(self.web_prefix), content_type="text/html", headers={"Cache-Control": "no-store"})

    async def _icon(self, request: web.Request) -> web.StreamResponse:
        name = "icon-512.png" if request.path.endswith("512.png") else "icon.png"
        return web.FileResponse(Path(__file__).parent / "static" / name, headers={"Cache-Control": "max-age=86400"})

    async def _manifest(self, request: web.Request) -> web.Response:
        return web.json_response({"name": "远窗", "short_name": "远窗", "start_url": f"{self.px}/", "display": "standalone",
                                  "background_color": "#d5dee5", "theme_color": "#d5dee5",
                                  "icons": [{"src": f"{self.px}/icon.png", "sizes": "180x180", "type": "image/png"},
                                            {"src": f"{self.px}/icon-512.png", "sizes": "512x512", "type": "image/png"}]})

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
""".replace(f"'{PREFIX}/", f"'{self.px}/")
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
        # 挂了子路径时根路径友好跳到挂载点；没挂就跳主界面（现状）
        raise web.HTTPFound(f"{self.web_prefix}/" if self.web_prefix else f"{self.px}/")

    async def _to_app(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{self.px}/")

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
                f'<li><a href="{self.px}/t/{html.escape(r["id"])}">{time.strftime("%m-%d %H:%M", time.localtime(last(r)))} · '
                f'{html.escape(r.get("request") or r.get("title") or "")}</a></li>' for r in rs[:30])
            more = f'<li class="meta">更早的 {len(rs) - 30} 轮略</li>' if len(rs) > 30 else ""
            rows.append(f'<div class="card"><a href="{self.px}/?s={num}"><b>{html.escape(name)}</b></a>'
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
        out = [f'<p class="meta"><a href="{self.px}/history">← 历史记录</a></p>',
               f"<h1>{html.escape(r['title'])}：{html.escape(r['request'] or r['label'])}</h1>",
               f"<p class='meta'>{html.escape(r['label'])} · {when} 起 · {mins} 分钟 · "
               f"{html.escape(display(Path(r['cwd'])))} · {html.escape(r.get('model') or '')}</p>"]
        if r.get("error"):
            out.append(f"<div class='card'><b>出错原因</b>：{html.escape(r['error'])}</div>")
        if r.get("final_text"):
            out.append(f"<h2>结果</h2><div class='card say'>{_md(r['final_text'])}</div>")
        if r.get("images"):
            out.append("<h2>截图</h2>" + "".join(f'<img src="{self.px}/t/{r["id"]}/img/{i}" loading="lazy">'
                                                for i in range(len(r["images"]))))
        if r.get("files"):
            out.append("<h2>交给你的文件</h2><ul>" + "".join(
                f'<li><a href="{self.px}/t/{r["id"]}/file/{i}">{html.escape(Path(f).name)}</a></li>'
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
        base = f"{self.web_prefix}/dl/{request.match_info['tid']}/{request.match_info['i']}/p/"
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
                       "url": f"{self.web_prefix}/dl/{r['id']}/{i}"} for i, f in enumerate(r.get("files", []))]
                out.append({"id": r["id"], "title": r.get("title", ""), "started": r.get("started"), "ended": r.get("ended"), "files": fs,
                            "checkpoint_before": r.get("checkpoint_before", ""), "checkpoint_after": r.get("checkpoint_after", ""), "codex_turn_id": r.get("codex_turn_id", ""), "model": r.get("model", ""), "effort": r.get("effort", ""), "nfiles": len(r.get("changed_files") or []), "ncmds": len(r.get("commands") or []),
                            "images": [f"{self.px}/t/{r['id']}/img/{i}" for i in range(len(r.get("images", [])))],
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
        owner = request.get("owner", "admin")
        fenced = owner != "admin"
        regrouped = False
        for member in b.registry.sessions.values():
            if member.owner == owner and member.task_group and not member.task_group_manual:
                group = task_groups.normalize(member.task_group)
                if group != member.task_group:
                    member.task_group = group
                    regrouped = True
        if regrouped:
            b.registry.save()

        for s in [x for x in b.registry.sessions.values() if not x.hidden and x.owner == owner]:
            if self._relay_latest(s).num != s.num:
                continue
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
            rows.append({"num": s.num, "name": self._relay_chain(s)[0].name, "cwd": display(Path(s.cwd)), "status": status, "unread": unread,
                         "detail": detail, "updated": s.updated, "steps": len(turn.steps) if turn else 0,
                         "engine": getattr(s, "engine", "claude"),
                         "task_group": s.task_group or self._relay_chain(s)[0].task_group, "task_group_manual": s.task_group_manual,
                         "pinned": s.pinned, "category": "", "role": s.role,
                         "suggest": s.awaiting_kind == "suggest" and status == "idle",
                         "last_step": turn.steps[-1] if turn and turn.steps else ""})
        order = {"awaiting": 0, "running": 1, "queued": 2, "error": 3, "paused": 4, "idle": 5}
        rows.sort(key=lambda r: (order[r["status"]], -r["updated"]))
        acct = request.get("acct", "")
        me = {"name": acct, "role": self.role(acct), "root": display(Path(self.root_of(acct)).expanduser()) if fenced else ""}
        return web.json_response({"sessions": rows, "recent": [] if fenced else self._recent_folders(), "last_folder": "" if fenced else self._last_folder(),
                                   "me": me,
                                  "folders": [] if fenced else list(b.cfg.folders),
                                  "current": None if fenced else b.registry.current,
                                  "models": [m for m in model_options() if not fenced or m[0] in ("opus", "sonnet", "haiku", "fable")],
                                  "limits": self._limits(b.runner.limits) if not fenced else self._my_limits(acct),
                                  "limits_gpt": [] if fenced else self._limits(getattr(b.runner, "limits_gpt", {})),
                                  "task_grouping": getattr(b.cfg, "task_grouping", False), "has_gpt": any(getattr(x, "engine", "") == "codex" for x in b.registry.visible(owner))})

    def _recent_folders(self, limit: int = 6) -> list[dict]:
        """最近用过的工作文件夹（新对话菜单里排在最前）；临时文件夹和家目录不算。这里不碰磁盘：受保护的文件夹一 stat 就可能卡住。"""
        b = self.bridge
        scratch = str(Path(b.cfg.scratch_root).expanduser())
        home = str(Path.home())
        seen, out = {str(Path(v).expanduser()) for v in b.cfg.folders.values()}, []   # 配了短名的文件夹已经在菜单里，不再重复列
        for s in sorted(b.registry.sessions.values(), key=lambda x: -x.updated):
            if s.role == "router":   # 总管的文件夹不是干活的地方
                continue
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
            if s.role == "router":   # 总管的文件夹不是干活的地方
                continue
            p = str(Path(s.cwd).expanduser())
            if p == home or p.startswith(scratch):
                return ""
            return next((k for k, v in b.cfg.folders.items() if str(Path(v).expanduser()) == p), p)
        return ""

    def _my_limits(self, acct: str) -> list[dict]:
        """受限账号只看自己的额度：用了自己那份的百分之几、几点重置。"""
        q = self.quota(acct)
        if not q:
            return []
        return [{"name": "本周", "pct": round(q["pct"]), "reset": q["reset"], "reset_at": q["reset_at"]}]

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
            for member in self._relay_chain(s):
                member.name, member.renamed = val[:40], True
        elif act == "task_group":
            if not 2 <= len(val) <= 24 or any(ord(c) < 32 for c in val):
                return web.json_response({"ok": False, "error": "任务分组名称需为2到24字"}, status=400)
            for member in self._relay_chain(s):
                member.task_group, member.task_group_manual = val, True
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
        if s.engine != "claude":
            key = ("x", len(list(self.turns_dir.glob(f"{s.num}-*.json"))))
        else:
            p = thread.transcript_path(s.claude_id) if s.claude_id else None
            key = ("c", p.stat().st_mtime if p else 0)
        hit = self._search_cache.get(s.num)
        if hit and hit[0] == key:
            return hit[1]
        items: list[dict] = []
        try:
            if s.engine != "claude":
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
        for s in sorted((s for s in b.registry.sessions.values() if s.owner == request.get("owner", "admin")), key=lambda x: -x.updated):
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

    def _relay_chain(self, session):
        chain, seen = [], set()
        while session and session.num not in seen:
            chain.append(session)
            seen.add(session.num)
            parent = self.bridge.registry.get(session.relay_from) if session.relay_from else None
            if not parent or parent.owner != session.owner:
                break
            session = parent
        return list(reversed(chain))

    def _relay_latest(self, session):
        seen = set()
        while session and session.relay_to and session.num not in seen:
            seen.add(session.num)
            child = self.bridge.registry.get(session.relay_to)
            if not child or child.owner != session.owner or child.num in seen:
                break
            session = child
        return session

    async def _saved_items(self, s):
        b, num = self.bridge, s.num
        # 两种引擎的附件都在完成后的归档里；归档可能晚于状态更新，须参与缓存失效。
        lt = b.runner.last.get(num) if s.engine != "claude" and hasattr(b.runner, "last") else None
        key = (s.claude_id or s.codex_id or s.acp_id, s.updated, len(list(self.turns_dir.glob(f"{num}-*.json"))),
               lt.ended if lt else 0, self._cc_mtime(s))
        cached = self._threads.get(num)
        if cached and cached[0] == key:
            items = cached[1]
        else:
            items = []
            if s.engine != "claude":
                items = thread.attach_records(self._codex_history(num), self._records(num))
            elif s.claude_id:
                try:
                    times: dict = {}
                    msgs = await asyncio.to_thread(thread.full_messages, s.claude_id, s.cwd, times)
                    items = thread.attach_records(thread.history(msgs, times=times), self._records(num))
                except Exception:  # noqa: BLE001 - 读不到记录就只显示进行中的
                    log.debug("读对话记录失败", exc_info=True)
            self._threads[num] = (key, items)
        return [dict(it) for it in items]

    async def _api_thread(self, request: web.Request) -> web.Response:
        b = self.bridge
        num = int(request.match_info["num"])
        s = b.registry.get(num)
        if not s:
            raise web.HTTPNotFound()
        latest = self._relay_latest(s)
        if latest.num != num:
            return web.json_response({"num": num, "redirect": latest.num})
        chain = self._relay_chain(s)
        items = await self._saved_items(s)
        # 你刚发的话：Claude 的记录要等这一轮做完才重读，先把还没出现在记录里的接在后面（不限时，进了记录才拿掉）
        # 带图的话在记录里是“[图片]\n原话”，比对时去掉图片占位、只看原话开头
        # 记录里你的话后面可能多了“（附件已存到：…）”，所以按开头比：记录里那句以你刚发的话开头就算进了记录
        # 只认发出之后才写进记录的那条：同一句话（比如“批准”按钮）再发一次，不会被旧的那条误认成已处理
        said = [(re.sub(r"^\[(?:\d+ 张)?图片\]\s*", "", it["text"].strip()), it.get("ts") or 0.0) for it in items if it["who"] == "you"]
        out = b.outgoing.get(num, [])
        out[:] = [o for o in out if not any(_said_has(x, o["text"]) and (not ts or ts >= o["t"] - 5) for x, ts in said)]
        if s.rewind_at:   # 点过“回到这里”：那条之后的先收起来，下一句从那里接着；还没说下一句前能撤销
            cut = next((i for i, it in enumerate(items) if it.get("end") == s.rewind_at), None)
            if cut is not None:
                items = items[:cut + 1] + [{"who": "note", "rewind": True, "fork": s.forking, "text": s.rewind_note or "已回到这里"}]
        items += [{"who": "you", "text": o["text"], "pending": True, "age": int(time.time() - o["t"])} for o in out]
        if s.role == "router" and s.notes:   # 总管还没看的“期间的情况”（含你用 #N 直接交出去的话）
            items.append({"who": "note", "text": f"期间的情况 · {len(s.notes)} 条（总管下次开口前会看）", "sa": "\n".join(f"- {n}" for n in s.notes)})
        history = []
        for index, previous in enumerate(chain[:-1]):
            history += [{**it, "source_num": previous.num, "historical": True}
                        for it in await self._saved_items(previous)]
            following = chain[index + 1]
            history.append({"who": "note", "text": "已接力，后续使用新的上下文", "handoff": following.relay_note})
        items = history + items
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
        last = next((it["text"] for it in reversed(items) if it["who"] == "claude" and not it.get("historical") and not NOISE.match(it["text"] or "")), "")
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
        relay_busy = bool(turn) or b.runner.busy(num)
        relaying = num in getattr(b, "_relaying", ())
        recommendation = relay_hint(s, relay_busy, relaying)
        if recommendation and recommendation["kind"] in ("long", "cold") and not s.relay_suggested:
            s.relay_suggested = True
            b.registry.save()
        usage = {"model": display_model(selected) if selected else "默认模型", "effort": EFFORT_CN.get(eff, eff or "默认"),
                 "mkey": "gpt" if s.engine == "codex" else s.engine if is_acp(s.engine) else next((k for k in ("opus", "sonnet", "haiku", "fable") if k in pick.lower()), ""),
                 "ekey": eff or "",
                 "context": round(s.context_pct) if s.context_pct is not None else None,
                 "tokens": s.context_tokens, "relay": recommendation, "relay_suggested": s.relay_suggested,
                 "compacting": b.runner.compacting(num), "busy": b.runner.busy(num)}
        queue = ["压缩上下文" if j.text == COMPACT else (t[:80] or "（图片）") for j in b.runner.queued(num) for t in (j.parts or [j.text])]
        limit = {"until": s.limit_until, "at": time.strftime("%H:%M", time.localtime(s.limit_until)), "auto": s.auto_resume} \
            if s.limit_until > time.time() and not turn else None
        five = b.runner.limits.get("five_hour")
        return web.json_response({"num": num, "name": chain[0].name, "cwd": display(Path(s.cwd)), "items": items,
                                  "live": live, "queue": queue, "awaiting": "" if turn else s.awaiting,
                                  "awaiting_kind": "" if turn else s.awaiting_kind, "decision": decision, "usage": usage,
                                  "limit": limit, "engine": s.engine,  "relay_to": s.relay_to or None,
                                  "can_relay": s.relay_suggested and bool(s.claude_id or s.codex_id) and not s.relay_to and not relay_busy and not relaying,
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

    def _inbox(self, request: web.Request) -> Path:
        """网页传上来的附件放哪：admin 放小E 自己的收件箱；受限账号放进它自己的文件夹（不然它读不到）。"""
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
            if hasattr(b, "registry") and (session := b.registry.get(int(num))):
                num = self._relay_latest(session).num
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
            await b._message(Cmd("message", num=int(num), text=text), inc, routed=True)
            return web.json_response({"ok": True, "injected": busy})
        root = Path(self.root_of(acct)).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        model, effort = self._model_effort(data)
        before = b.registry.reserve_num()
        b.record(f"你（网页 · {acct}）", text, images=len(images), num=before)
        await b._start(before, root, [], text, inc, kind="simple", model=None if model == "gpt" or is_acp(model or "") else model, effort=effort,
                       owner=owner, root=str(root))
        return web.json_response({"ok": True, "num": before})

    @staticmethod
    def _model_effort(data: dict) -> tuple[str | None, str | None]:
        """网页上选的模型（opus / sonnet / haiku）和深度（low…max）；没选或不认得的返回 None。"""
        from .app import EFFORTS, MODELS
        m = str(data.get("model") or "")
        return ("gpt" if m == "gpt" else m if is_acp(m) else MODELS.get(m)), next((v for v in EFFORTS.values() if v and v == data.get("effort")), None)

    async def _api_settings(self, request: web.Request) -> web.Response:
        """改某个对话的模型和思考深度，下一轮起生效（正在做的这一轮不变）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        s = self.bridge.registry.get(int(request.match_info["num"]))
        if not s:
            raise web.HTTPNotFound()
        model, effort = self._model_effort(await request.json())
        if model == "gpt" or is_acp(model or "") or (s.engine != "claude" and model):
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
        if num and not self.bridge.registry.mine(num, request.get("owner", "admin")):
            return web.json_response({"ok": False}, status=404)
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
        if is_acp(p.engine):
            return web.json_response({"ok": False, "error": f"{ACP_ENGINES[p.engine]['name']} 的对话暂不支持分叉"})
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
            s = Session(num=b.registry.reserve_num(), name=p.name[:8] + "·分叉", cwd=p.cwd, owner=p.owner, root=p.root,
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
                    owner=p.owner, root=p.root, claude_id=p.claude_id if to else None, model=p.model, effort=p.effort, renamed=True, titled=True,
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
        if is_acp(s.engine):
            return web.json_response({"ok": False, "error": f"{ACP_ENGINES[s.engine]['name']} 的对话暂不支持回退"})
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

    def _sig_sessions(self, owner: str = "admin") -> str:
        """对话列表的“指纹”：谁在做、做到第几步、等不等你、更新时间；变了就叫网页刷新列表。"""
        b = self.bridge
        out = []
        for s in b.registry.sessions.values():
            if s.owner != owner:
                continue
            t = b.runner.turns.get(s.num)
            out.append((s.num, s.updated, s.awaiting, s.seen > s.updated, bool(t), len(t.steps) if t else 0,
                        bool(t and t.confirm), b.runner.busy(s.num), s.hidden, s.pinned, s.name, s.task_group, s.task_group_manual, s.limit_until,
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
        return json.dumps([s.updated if s else 0, s.relay_to if s else 0, len(t.timeline) if t else -1, bool(t and t.confirm),
                           len(b.outgoing.get(num, [])), recs, s.rewind_at if s else "", len(b.runner.queued(num)),
                           s.limit_until if s else 0, s.auto_resume if s else True, cc_mtime])

    async def _api_events(self, request: web.Request) -> web.StreamResponse:
        """实时推送（Server-Sent Events）：列表变了发 s，打开的对话有新步骤 / 做完了发 t，正在写的回复发 p（带全文）。
        网页收到就当场更新，不用每几秒问一次。每 15 秒发个空行保活（反向代理 120 秒没动静会断）。"""
        num = int(request.query.get("num") or 0)
        owner = request.get("owner", "admin")
        if num and not self.bridge.registry.mine(num, owner):
            num = 0
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        last_s = last_t = last_p = None
        beat = time.time()
        try:
            while True:
                sig_s = self._sig_sessions(owner)
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

    async def _api_relay(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        num = int(request.match_info["num"])
        s = self.bridge.registry.get(num)
        if not s:
            raise web.HTTPNotFound()
        if num not in self.bridge._relaying:
            if self.bridge.runner.busy(num):
                return web.json_response({"ok": False, "message": "正在回答或还有排队消息，请等回答结束后再接力。"}, status=409)
            if not s.relay_suggested and not relay_hint(s, False, False):
                return web.json_response({"ok": False, "message": "这个对话暂时没有接力建议。"}, status=409)
        msg = self.bridge.relay(num, via="web")
        return web.json_response({"ok": True, "message": msg})

    async def _api_stop(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        stopped = await self.bridge.runner.stop(int(request.match_info["num"]))
        return web.json_response({"ok": True, "stopped": stopped})

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

    # ---------- 启动 ----------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.path in self.public:
            return await handler(request)
        if not self._logged_in(request):
            return self._denied(request)
        if "acct" not in request:   # 测试里直接放行登录的：当作 admin
            request["acct"], request["owner"] = self._admin(), "admin"
        owner, admin = request["owner"], self.role(request["acct"]) == "admin"
        route = request.match_info.route.resource.canonical if request.match_info.route.resource else ""
        if not admin and route not in self.user_routes:
            raise web.HTTPForbidden(text="这个账号用不了这里")
        if route.startswith(f"{self.px}/admin") or route.startswith(f"{self.px}/api/admin"):
            if not admin:
                raise web.HTTPForbidden(text="只有管理员能看")
        # 带对话编号的：只能是自己账号的对话（别人的当作没有）
        raw = request.match_info.get("num") or (request.match_info.get("tid") or "").split("-")[0]
        if raw.isdigit() and self.bridge is not None and hasattr(self.bridge, "registry"):
            s = self.bridge.registry.get(int(raw))
            if (s and s.owner != owner) or (not s and not admin):
                raise web.HTTPNotFound()
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard], client_max_size=0)
        app.cleanup_ctx.append(self._speech_context)
        P = self.px   # /_x 的实际地址：空前缀时就是 /_x，挂了子路径时是 {web_prefix}/_x
        app.router.add_get(f"{P}/login", self._login_get)
        app.router.add_post(f"{P}/login", self._login_post)
        app.router.add_get("/", self._to_index)
        app.router.add_get(f"{P}/", self._app_page)
        app.router.add_get(f"{P}/history", self._index)
        app.router.add_get(f"{P}/icon.png", self._icon)
        app.router.add_get(f"{P}/icon-512.png", self._icon)
        app.router.add_get(f"{P}/manifest.json", self._manifest)
        app.router.add_get(f"{P}/sw.js", self._sw)
        app.router.add_get(f"{P}/health", self._health)
        app.router.add_get(f"{P}/api/push/key", self._api_push_key)
        app.router.add_post(f"{P}/api/push", self._api_push)
        app.router.add_post(f"{P}/api/logout", self._api_logout)
        app.router.add_get(f"{P}/api/devices", self._api_devices)
        app.router.add_post(f"{P}/api/devices/logout", self._api_device_logout)
        app.router.add_get(f"{P}/api/sessions", self._api_sessions)
        app.router.add_post(f"{P}/api/task-groups", self._api_task_groups)
        app.router.add_get(f"{P}/api/events", self._api_events)
        app.router.add_get(f"{P}/api/uimg/{{num:\\d+}}/{{uuid}}/{{k:\\d+}}", self._api_uimg)
        app.router.add_get(f"{P}/api/thread/{{num:\\d+}}", self._api_thread)
        app.router.add_post(f"{P}/api/manage/{{num:\\d+}}", self._api_manage)
        app.router.add_post(f"{P}/api/speech/{{num:\\d+}}", self._api_speech)
        app.router.add_post(f"{P}/api/speech/release", self._release_speech)
        app.router.add_get(f"{P}/api/audio/{{key}}", self._api_audio)
        app.router.add_post(f"{P}/api/audio/{{key}}", self._api_audio)
        app.router.add_get(f"{P}/audio/{{key}}.mp3", self._audio)
        app.router.add_get(f"{P}/api/search", self._api_search)
        app.router.add_post(f"{P}/api/send", self._api_send)
        app.router.add_get(f"{P}/api/schedules", self._api_schedules)
        app.router.add_post(f"{P}/api/schedules", self._api_schedules)
        app.router.add_post(f"{P}/api/settings/{{num}}", self._api_settings)
        app.router.add_post(f"{P}/api/compact/{{num:\\d+}}", self._api_compact)
        app.router.add_post(f"{P}/api/relay/{{num:\\d+}}", self._api_relay)
        app.router.add_post(f"{P}/api/confirm/{{num:\\d+}}", self._api_confirm)
        app.router.add_post(f"{P}/api/stop/{{num:\\d+}}", self._api_stop)
        app.router.add_post(f"{P}/api/rewind/{{num:\\d+}}", self._api_rewind)
        app.router.add_post(f"{P}/api/fork/{{num:\\d+}}", self._api_fork)
        app.router.add_get(f"{P}/api/browse", self._api_browse)
        app.router.add_get(f"{P}/api/cc-sessions", self._api_cc_sessions)
        app.router.add_post(f"{P}/api/resume-cc", self._api_resume_cc)
        app.router.add_post(f"{P}/api/unqueue/{{num:\\d+}}", self._api_unqueue)
        app.router.add_post(f"{P}/api/presence", self._api_presence)
        app.router.add_get(f"{P}/admin", self._admin_page)
        app.router.add_get(f"{P}/api/admin", self._api_admin)
        app.router.add_post(f"{P}/api/admin", self._api_admin_act)
        app.router.add_get(P, self._to_app)
        app.router.add_get(f"{P}/t/{{tid}}", self._turn_page)
        app.router.add_get(f"{P}/t/{{tid}}/img/{{i:\\d+}}", self._turn_image)
        app.router.add_get(f"{P}/t/{{tid}}/file/{{i:\\d+}}", self._turn_file)
        # 网页里下文件走 /_x 外面的地址：iPhone、iPad 从主屏幕打开的远窗，点范围外的链接会弹出带“完成”的浏览页，
        # 看完能回来；范围内的链接直接在远窗窗口里打开文件，没有返回键（10-27 遇到）
        app.router.add_get(f"{self.web_prefix}/dl/{{tid}}/{{i:\\d+}}", self._turn_file)
        app.router.add_get(f"{self.web_prefix}/dl/{{tid}}/{{i:\\d+}}/pages", self._pdf_pages)
        app.router.add_get(f"{self.web_prefix}/dl/{{tid}}/{{i:\\d+}}/p/{{name}}", self._pdf_page)
        # 被反代透传挂到子路径时，挂载点本身也要能落到主界面。
        # 带不带尾斜杠都要注册：浏览器/导航进来的是无斜杠形态，aiohttp 不会自动补斜杠，
        # 已登录用户（守卫放行）会直接 404，未登录的只会被守卫弹走测不出来。
        if self.web_prefix:
            app.router.add_get(self.web_prefix, self._to_app)
            app.router.add_get(f"{self.web_prefix}/", self._to_app)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        log.info("远窗网页在 127.0.0.1:%d", self.port)
