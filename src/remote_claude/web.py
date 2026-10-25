"""小E 网页：完整过程页和试课预览。只听本机端口，经 SSH 反向隧道挂到阿里云（如 https://xiaoe.ericlab.cn）。

进门：固定网址 + 账号密码，登录一次这台设备 30 天免登录（Cookie）。密码只存 PBKDF2 指纹（钥匙串），
同一来源连错 5 次锁 15 分钟、所有来源一小时内错 30 次全体锁一小时。
试课预览只转本机 CyberScope 的课程预览页、静态文件和预览要用的几个接口；教师台、学生数据、WebSocket 一律不转。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import shutil
import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from . import thread, translate
from .push import Push
from .cards import parse_decision
from .commands import Cmd
from .dingtalk import Incoming
from .folders import display
from .report import EFFORT_CN, LIMIT_CN, model_name

log = logging.getLogger(__name__)

COOKIE = "xiaoe_sid"
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

PREFIX = "/_x"  # 小E 自己的页面都在这下面，其余路径留给试课预览

# 试课预览放行的接口：判题、草稿课列表、提词与参考答案（后两者后端要教师登录，外网没有就自然拒绝）、心跳与前端日志
PREVIEW_API = ("/api/lesson-preview/", "/api/drafts", "/api/teacher/lecture-cues", "/api/teacher/answers",
               "/api/ping", "/api/logs", "/api/fault")
PREVIEW_PAGES = {"/studio-preview.html", "/lecture-preview.html"}
COURSE_DIRS = ("/workspace/", "/courses/")  # 课件本身（草稿在 /workspace/，上架的在 /courses/）
# CyberScope 只让“本机直连”读做课工作区（/workspace/ 课件、判题、草稿课列表），带代理来源头的一律当外来（auth.is_trusted_local）。
# 进得了这里的人已经过了小E 的一次性链接登录，所以只对这三类请求去掉来源头；其余（含教师接口）照旧带着，后端照旧当外来。
LOCAL_ONLY = ("/workspace/", "/api/lesson-preview/", "/api/drafts")
PROXY_SOURCE = {"x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host", "x-forwarded-proto"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "cookie", "content-length"}


def preview_allowed(method: str, path: str) -> bool:
    if ".." in path or path.startswith("/ws"):
        return False
    if path.startswith("/api/"):
        return any(path == a or (a.endswith("/") and path.startswith(a)) or path.startswith(a + "/")
                   for a in PREVIEW_API)
    if method not in ("GET", "HEAD"):
        return False
    if path == "/" or path.endswith(".html"):
        # 平台页面只放两张预览页；课件里每一节的内容也是 .html（fragments/section-N.html、课的 index.html），放行课程目录下的
        return path in PREVIEW_PAGES or path.startswith(COURSE_DIRS)
    return True


def course_previews(files: list[str], root: str) -> list[tuple[str, str]]:
    """改到的文件属于课程根里的哪几节课 → (课名, 预览地址)。"""
    if not root:
        return []
    base = Path(root).expanduser().resolve()
    out: dict[str, str] = {}
    for f in files:
        p = Path(f).expanduser()
        try:
            p.resolve().relative_to(base)
        except ValueError:
            continue
        for d in [p.parent, *p.parents]:
            if d == base or base not in d.resolve().parents:
                break
            if (d / "course.json").is_file():
                rel = d.resolve().relative_to(base).as_posix()
                out[d.name] = f"/studio-preview.html?course={quote(f'/workspace/{rel}/course.json')}"
                break
    return list(out.items())


def _md(text: str) -> str:
    """回复里的 Markdown 粗略转成 HTML：转义后只处理加粗、行内代码和链接，其余保留换行。"""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e5e5e2;--acc:#2f6fdf}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--card:#1f1f1f;--ink:#ececec;--mute:#9a9a9a;--line:#333;--acc:#7aa7ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 -apple-system,"PingFang SC",sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:8px 0 4px}} h2{{font-size:16px;margin:24px 0 8px;color:var(--mute)}}
.meta{{color:var(--mute);font-size:14px}} a{{color:var(--acc)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:8px 0}}
.say{{white-space:pre-wrap}} code{{font:13px ui-monospace,Menlo,monospace;background:rgba(127,127,127,.12);padding:1px 4px;border-radius:4px}}
pre{{white-space:pre-wrap;word-break:break-all;font:13px/1.5 ui-monospace,Menlo,monospace;margin:6px 0 0}}
.step{{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--line)}} .step:last-child{{border:0}}
.t{{color:var(--mute);font:12px ui-monospace,Menlo,monospace;min-width:58px;padding-top:3px}}
details summary{{cursor:pointer}} img{{max-width:100%;border-radius:8px;border:1px solid var(--line);margin:6px 0}}
.btn{{display:inline-block;background:var(--acc);color:#fff;text-decoration:none;padding:8px 14px;border-radius:8px;margin:4px 6px 4px 0}}
ul{{padding-left:20px;margin:4px 0}}
</style></head><body><main>{body}</main></body></html>"""


class Web:
    def __init__(self, base: str, port: int, data_dir: Path, preview_upstream: str = "", preview_root: str = "",
                 user: str = "", pw_hash: str = ""):
        self.user, self.pw_hash = user, pw_hash
        self.fails: dict[str, list[float]] = {}
        self.all_fails: list[float] = []
        self.base = base.rstrip("/")
        self.port = port
        self.preview_upstream = preview_upstream.rstrip("/")
        self.preview_root = preview_root
        self.turns_dir = data_dir / "turns"
        self.store_path = data_dir / "web.json"
        self.push = Push(data_dir / "push.json", self.base or "https://localhost")
        self.store = self._load()
        self._client: aiohttp.ClientSession | None = None
        self.bridge = None  # 小E 本体（app.Bridge），网页对话框靠它看对话、发话
        self._threads: dict[int, tuple[tuple, list[dict]]] = {}
        self._zh_path = data_dir / "translations.json"
        try:
            self._zh: dict[str, str] = json.loads(self._zh_path.read_text())
        except (OSError, json.JSONDecodeError):
            self._zh = {}
        self._zh_busy: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    # ---------- 登录 ----------
    def _load(self) -> dict:
        try:
            d = json.loads(self.store_path.read_text())
        except (OSError, json.JSONDecodeError):
            d = {}
        return {"sessions": d.get("sessions", {})}

    def _save(self) -> None:
        now = time.time()
        self.store["sessions"] = {k: v for k, v in self.store["sessions"].items() if v > now}
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
        return bool(sid) and self.store["sessions"].get(sid, 0) > time.time()

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
        body = f"""<style>
main{{min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:24px}}
.login{{width:100%;max-width:360px}} .login h1{{font-size:26px;margin:0 0 6px}} .login .sub{{color:var(--mute);margin:0 0 24px}}
.login input{{width:100%;font:inherit;font-size:17px;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 12px;outline:none}}
.login input:focus{{border-color:var(--acc)}}
.login button{{width:100%;font:inherit;font-size:17px;font-weight:600;color:#fff;background:var(--acc);border:0;border-radius:12px;padding:12px;margin-top:4px}}
.err{{color:#c2410c;margin:0 0 12px}}
</style>
<form class="login" method="post" action="{PREFIX}/login">
<h1>小E</h1><p class="sub">登录一次，这台设备 30 天免登录</p>{note}
<input type="hidden" name="next" value="{html.escape(nxt)}">
<input name="username" placeholder="账号" autocomplete="username" inputmode="numeric" required>
<input name="password" type="password" placeholder="密码" autocomplete="current-password" required>
<button>登录</button></form>"""
        return self._page("登录 · 小E", body, status)

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
        self._save()
        resp = web.HTTPFound(nxt)
        resp.set_cookie(COOKIE, sid, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax",
                        secure=self.base.startswith("https://"), path="/")
        raise resp

    # ---------- 记下一轮的完整过程 ----------
    def save_turn(self, label: str, turn, cwd: str, title: str) -> str:
        tid = f"{label.split()[0].lstrip('#')}-{int(turn.started)}"
        files = list(dict.fromkeys(turn.changed_files))
        # 交给你的截图和文件复制一份到小E 自己的文件夹（原处可能是临时目录，过后会被清掉）
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
            "started": turn.started, "ended": turn.ended, "model": turn.model, "effort": turn.effort,
            "final_text": turn.final_text, "error": turn.error, "timeline": turn.timeline,
            "changed_files": files, "commands": turn.commands, "images": kept_images, "files": kept_files,
            "previews": course_previews(files, self.preview_root), "translated": turn.translated,
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
        return web.json_response({"detail": "需要先登录小E 网页"}, status=401)

    async def _app_page(self, request: web.Request) -> web.Response:
        from .webui import app_html
        return web.Response(text=app_html(), content_type="text/html", headers={"Cache-Control": "no-store"})

    async def _icon(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(Path(__file__).parent / "static" / "icon.png", headers={"Cache-Control": "max-age=86400"})

    async def _manifest(self, request: web.Request) -> web.Response:
        return web.json_response({"name": "小E", "short_name": "小E", "start_url": f"{PREFIX}/", "display": "standalone",
                                  "background_color": "#fafaf9", "theme_color": "#fafaf9",
                                  "icons": [{"src": f"{PREFIX}/icon.png", "sizes": "180x180", "type": "image/png"}]})

    async def _sw(self, request: web.Request) -> web.Response:
        # 手机通知的后台脚本：收到推送就弹通知，点通知打开那个对话
        js = """self.addEventListener('push', e => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(self.registration.showNotification(d.title || '小E', {body: d.body || '', tag: d.tag || undefined, data: {url: d.url || '/_x/'}, icon: '/_x/icon.png'}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/_x/';
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(cs => {
    for (const c of cs) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    return clients.openWindow(url);
  }));
});
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
"""
        return web.Response(text=js, content_type="application/javascript", headers={"Cache-Control": "no-cache"})

    async def _api_push_key(self, request: web.Request) -> web.Response:
        return web.json_response({"key": await asyncio.to_thread(self.push.public_key)})

    async def _api_push(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        try:
            self.push.add(await request.json())
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        return web.json_response({"ok": True})

    async def _to_index(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{PREFIX}/")

    async def _index(self, request: web.Request) -> web.Response:
        rows = []
        files = sorted(self.turns_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
        for p in files:
            try:
                r = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            when = time.strftime("%m-%d %H:%M", time.localtime(r["ended"] or r["started"]))
            pv = "".join(f' · <a href="{html.escape(u)}">试课 {html.escape(n)}</a>' for n, u in r.get("previews", []))
            rows.append(f'<div class="card"><a href="{PREFIX}/t/{r["id"]}">{html.escape(r["label"])} · '
                        f'{html.escape(r["request"] or "")}</a><div class="meta">{when} · {html.escape(r["title"])}{pv}</div></div>')
        drafts = []
        root = Path(self.preview_root).expanduser() if self.preview_root else None
        if root and (root / "_drafts").is_dir():
            for cj in sorted((root / "_drafts").glob("*/course.json"), key=lambda p: p.parent.stat().st_mtime, reverse=True):
                try:
                    title = json.loads(cj.read_text()).get("title") or cj.parent.name
                except (OSError, json.JSONDecodeError):
                    title = cj.parent.name
                url = f"/studio-preview.html?course={quote(f'/workspace/_drafts/{cj.parent.name}/course.json')}"
                drafts.append(f'<a class="btn" href="{html.escape(url)}">试课：{html.escape(title)}</a>')
        top = f"<h1>在做的课</h1><p>{''.join(drafts)}</p>" if drafts else ""
        return self._page("小E · 最近的活", top + "<h1>最近的活</h1>" + ("".join(rows) or "<p class='meta'>还没有记录。</p>"))

    async def _turn_page(self, request: web.Request) -> web.Response:
        r = self._turn(request.match_info["tid"])
        if not r:
            return self._page("没有这一轮", "<h1>没有这一轮的记录</h1>", 404)
        mins = max(1, round(((r["ended"] or r["started"]) - r["started"]) / 60))
        when = time.strftime("%m-%d %H:%M", time.localtime(r["started"]))
        out = [f'<p class="meta"><a href="{PREFIX}/">← 最近的活</a></p>',
               f"<h1>{html.escape(r['title'])}：{html.escape(r['request'] or r['label'])}</h1>",
               f"<p class='meta'>{html.escape(r['label'])} · {when} 起 · {mins} 分钟 · "
               f"{html.escape(display(Path(r['cwd'])))} · {html.escape(r.get('model') or '')}</p>"]
        if r.get("previews"):
            out.append("<p>" + "".join(f'<a class="btn" href="{html.escape(u)}">试课：{html.escape(n)}</a>'
                                       for n, u in r["previews"]) + "</p>")
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
        return web.FileResponse(r["files"][i], headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    # ---------- 试课预览：转给本机 CyberScope ----------
    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        if not self.preview_upstream or not preview_allowed(request.method, request.path):
            return self._page("不开放", "<h1>这个地址不开放</h1><p class='meta'>外网只能打开课程预览。</p>", 403)
        if self._client is None:
            self._client = aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=120))
        drop = HOP | (PROXY_SOURCE if request.path.startswith(LOCAL_ONLY) else set())
        headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        try:
            async with self._client.request(request.method, self.preview_upstream + request.rel_url.path_qs,
                                            headers=headers, data=await request.read(), allow_redirects=False) as up:
                resp = web.StreamResponse(status=up.status, headers={
                    k: v for k, v in up.headers.items() if k.lower() not in HOP | {"set-cookie"}})
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(64 * 1024):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except aiohttp.ClientError:
            return self._page("预览没开", "<h1>本机的课程预览没开</h1><p class='meta'>Mac 上的 CyberScope（8000 端口）"
                              "没在运行。在钉钉里让小E 帮你启动，或回到电脑前双击“做课.command”。</p>", 502)

    # ---------- 网页对话框的接口 ----------
    def _records(self, num: int) -> list[dict]:
        out = []
        for p in sorted(self.turns_dir.glob(f"{num}-*.json"), key=lambda p: int(p.stem.split("-")[1])):
            try:
                r = json.loads(p.read_text())
                fs = [{"name": Path(f).name, "size": Path(f).stat().st_size if Path(f).is_file() else 0,
                       "url": f"{PREFIX}/t/{r['id']}/file/{i}"} for i, f in enumerate(r.get("files", []))]
                out.append({"id": r["id"], "title": r.get("title", ""), "previews": r.get("previews", []), "files": fs,
                            "model": r.get("model", ""),
                            "images": [f"{PREFIX}/t/{r['id']}/img/{i}" for i in range(len(r.get("images", [])))],
                            "final_text": r.get("final_text", ""), "translated": r.get("translated", "")})
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return out

    def _drafts(self) -> list[dict]:
        root = Path(self.preview_root).expanduser() if self.preview_root else None
        if not root or not (root / "_drafts").is_dir():
            return []
        out = []
        for cj in sorted((root / "_drafts").glob("*/course.json"), key=lambda p: p.parent.stat().st_mtime, reverse=True):
            try:
                title = json.loads(cj.read_text()).get("title") or cj.parent.name
            except (OSError, json.JSONDecodeError):
                title = cj.parent.name
            out.append({"title": title, "dir": cj.parent.name, "mtime": int(cj.parent.stat().st_mtime),
                        "url": f"/studio-preview.html?course={quote(f'/workspace/_drafts/{cj.parent.name}/course.json')}"})
        return out

    async def _api_sessions(self, request: web.Request) -> web.Response:
        b = self.bridge
        now = time.time()
        rows = []
        for s in b.registry.visible():
            turn = b.runner.turns.get(s.num)
            if turn:
                status, detail = "running", f"在做 · {int((now - turn.started) // 60)} 分钟 · 第 {len(turn.steps)} 步"
            elif b.runner.busy(s.num):
                status, detail = "queued", "排队中"
            elif s.awaiting:
                status, detail = "awaiting", f"等你回答：{s.awaiting}"
            elif s.status == "error":
                status, detail = "error", "上次出错了"
            else:
                status, detail = "idle", s.last_title or "空闲"
            unread = status in ("idle", "awaiting", "error") and bool(s.claude_id) and s.updated > s.seen + 1
            rows.append({"num": s.num, "name": s.name, "cwd": display(Path(s.cwd)), "status": status, "unread": unread,
                         "detail": detail, "updated": s.updated, "steps": len(turn.steps) if turn else 0,
                         "last_step": turn.steps[-1] if turn and turn.steps else ""})
        order = {"awaiting": 0, "running": 1, "queued": 2, "error": 3, "idle": 4}
        rows.sort(key=lambda r: (order[r["status"]], -r["updated"]))
        return web.json_response({"sessions": rows, "drafts": self._drafts(),
                                  "folders": list(b.cfg.folders), "current": b.registry.current,
                                  "limits": [{"name": LIMIT_CN.get(k, k), "pct": round(u * 100), "reset": r or ""}
                                             for k, (u, r) in b.runner.limits.items()]})

    async def _api_thread(self, request: web.Request) -> web.Response:
        b = self.bridge
        num = int(request.match_info["num"])
        s = b.registry.get(num)
        if not s:
            raise web.HTTPNotFound()
        key = (s.claude_id, s.updated)
        cached = self._threads.get(num)
        if cached and cached[0] == key:
            items = cached[1]
        else:
            items = []
            if s.claude_id:
                try:
                    msgs = await asyncio.to_thread(thread.full_messages, s.claude_id, s.cwd)
                    items = thread.attach_records(thread.history(msgs), self._records(num))
                except Exception:  # noqa: BLE001 - 读不到记录就只显示进行中的
                    log.debug("读对话记录失败", exc_info=True)
            self._threads[num] = (key, items)
        items = [dict(it) for it in items]
        # 你刚发的话：Claude 的记录要等这一轮做完才重读，先把还没出现在记录里的接在后面（不限时，进了记录才拿掉）
        said = {it["text"].strip()[:40] for it in items if it["who"] == "you"}
        out = b.outgoing.get(num, [])
        out[:] = [o for o in out if o["text"].strip()[:40] not in said]
        items += [{"who": "you", "text": o["text"], "pending": True} for o in out]
        for it in items:
            if it["who"] == "claude":
                it["text"] = self._zh_or_later(it["text"])
        for it in [i for i in items if i["who"] == "claude"][-2:]:   # 最近两轮的工作过程也译（再早的不译，太费）
            if it.get("process"):
                it["process"] = [dict(e, text=self._zh_or_later(e["text"])) if e["k"] in ("think", "say") else e
                                 for e in it["process"]]
        turn = b.runner.turns.get(num)
        if not turn and s.updated > s.seen:   # 打开了、看到最新一轮了，就不算没看
            s.seen = time.time()
            b.registry.save()
        live = None
        if turn:
            proc = [{"k": e["kind"], "text": str(e.get("text", ""))[:1200], "detail": str(e.get("detail") or "")[:400]}
                    for e in turn.timeline if e["kind"] in ("think", "say", "do")][-40:]
            for e in proc:
                if e["k"] in ("think", "say"):
                    e["text"] = self._zh_or_later(e["text"])
            live = {"request": turn.request, "minutes": int((time.time() - turn.started) // 60),
                    "total": len(turn.steps), "steps": turn.steps[-8:], "injected": turn.injected, "process": proc}
        last = next((it["text"] for it in reversed(items) if it["who"] == "claude"), "")
        d = parse_decision(last, loose=True) if s.awaiting and not turn else None
        decision = {"question": d.question, "options": d.options, "recommend": d.recommend} if d else None
        recs = self._records(num)
        mid = (turn.model if turn and turn.model else "") or (recs[-1].get("model") if recs else "") or s.model or ""
        eff = s.effort or b.runner.default_effort
        pick = s.model or mid
        usage = {"model": model_name(mid) if mid else "默认模型", "effort": EFFORT_CN.get(eff, eff or "默认"),
                 "mkey": next((k for k in ("opus", "sonnet", "haiku", "fable") if k in pick), ""), "ekey": eff or "",
                 "context": round(s.context_pct) if s.context_pct is not None else None}
        return web.json_response({"num": num, "name": s.name, "cwd": display(Path(s.cwd)), "items": items,
                                  "live": live, "awaiting": "" if turn else s.awaiting, "decision": decision, "usage": usage})

    def _zh_or_later(self, text: str) -> str:
        """英文的先原样给，后台译好存起来，下次刷新就是中文。"""
        if not translate.needs_translation(text):
            return text
        key = hashlib.sha1(text.encode()).hexdigest()
        if key in self._zh:
            return self._zh[key]
        if key not in self._zh_busy and len(self._zh_busy) < 6:
            self._zh_busy.add(key)
            asyncio.create_task(self._translate(key, text))
        return text

    async def _translate(self, key: str, text: str) -> None:
        """网页里显示的英文回复后台译成中文（和钉钉同一套：DeepSeek，Haiku 兜底），译好存起来，下次刷新就是中文。"""
        try:
            if done := await translate.to_chinese(text):
                self._zh[key] = done[0]
                self._zh = dict(list(self._zh.items())[-2000:])
                self._zh_path.write_text(json.dumps(self._zh, ensure_ascii=False))
        finally:
            self._zh_busy.discard(key)

    def _same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        return origin is None or origin == self.base or not self.base.startswith("https://")

    async def _save_files(self, data: dict) -> list[str]:
        """网页传上来的文件（任何格式）存进收件箱，原文件名保留在后面。"""
        paths = []
        folder = Path(self.store_path.parent) / "inbox" / "web"
        folder.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate((data.get("files") or [])[:10]):
            name = Path(str(f.get("name") or "file")).name.replace("/", "_")[:120] or "file"
            p = folder / f"{int(time.time() * 1000)}-{i}-{name}"
            p.write_bytes(base64.b64decode(str(f.get("data", "")).partition(",")[2]))
            paths.append(str(p))
        return paths

    async def _save_images(self, data: dict) -> list[str]:
        paths = []
        folder = Path(self.store_path.parent) / "inbox" / "web"
        folder.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate((data.get("images") or [])[:6]):
            head, _, b64 = str(url).partition(",")
            ext = "jpg" if "jpeg" in head else "png"
            p = folder / f"{int(time.time() * 1000)}-{i}.{ext}"
            p.write_bytes(base64.b64decode(b64))
            paths.append(str(p))
        return paths

    async def _api_send(self, request: web.Request) -> web.Response:
        """在某个对话里说话（它正在做就是插话）；没给 num 就是新开一个对话。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        data = await request.json()
        text = str(data.get("text") or "").strip()
        images = await self._save_images(data)
        uploaded = await self._save_files(data)
        if uploaded:
            text = (text + "\n\n" if text else "") + "\n".join(f"（附件已存到：{p}）" for p in uploaded)
        if not text and not images:
            return web.json_response({"ok": False, "error": "没写内容"}, status=400)
        b = self.bridge
        inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=text,
                       local_images=images)
        b.record("你（网页）", text, images=len(images), num=data.get("num"))
        num = data.get("num")
        if num:
            busy = int(num) in b.runner.turns
            await b._message(Cmd("message", num=int(num), text=text), inc, routed=True)
            return web.json_response({"ok": True, "injected": busy})
        folder = str(data.get("folder") or "").strip()
        kind = None
        course = str(data.get("course") or "").strip()
        if course and course in {d["dir"] for d in self._drafts()}:
            # 接着做某门课：在课程根里开，并说明是哪门课，免得它去猜
            title = next(d["title"] for d in self._drafts() if d["dir"] == course)
            folder, kind = str(Path(self.preview_root).expanduser()), "dev"
            inc.text = text = f"（这件事是关于课程《{title}》，目录 _drafts/{course}/）\n{text}"
        elif folder == "auto":
            # 让小E 看话判断在哪做；判断不出或不涉及文件夹就用空白的临时文件夹
            from . import router
            r = await router.route(text, [], None, list(b.cfg.folders))
            from . import folders as fd
            folder = r.folder if r and r.folder and fd.candidates(r.folder, b.cfg) else ""
            kind = r.kind if r else None
        elif folder == "scratch":
            folder = ""
        model, effort = self._model_effort(data)
        before = b.registry.next_num
        await b._new([folder] if folder else [], text, inc, note="（网页里新开）", kind=kind, model=model, effort=effort)
        return web.json_response({"ok": True, "num": before if b.registry.get(before) else None})

    @staticmethod
    def _model_effort(data: dict) -> tuple[str | None, str | None]:
        """网页上选的模型（opus / sonnet / haiku）和深度（low…max）；没选或不认得的返回 None。"""
        from .app import EFFORTS, MODELS
        return MODELS.get(str(data.get("model") or "")), next((v for v in EFFORTS.values() if v and v == data.get("effort")), None)

    async def _api_settings(self, request: web.Request) -> web.Response:
        """改某个对话的模型和思考深度，下一轮起生效（正在做的这一轮不变）。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        s = self.bridge.registry.get(int(request.match_info["num"]))
        if not s:
            raise web.HTTPNotFound()
        model, effort = self._model_effort(await request.json())
        s.model = model or s.model
        s.effort = effort or s.effort
        self.bridge.registry.save()
        self._threads.pop(s.num, None)
        return web.json_response({"ok": True})

    async def _api_compact(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        msg = await self.bridge.compact(int(request.match_info["num"]))
        return web.json_response({"ok": True, "message": msg})

    async def _api_stop(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        stopped = await self.bridge.runner.stop(int(request.match_info["num"]))
        return web.json_response({"ok": True, "stopped": stopped})

    # ---------- 启动 ----------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.path in (f"{PREFIX}/login", f"{PREFIX}/icon.png", f"{PREFIX}/manifest.json", f"{PREFIX}/sw.js"):
            return await handler(request)
        if not self._logged_in(request):
            return self._denied(request)
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard], client_max_size=150 * 1024 * 1024)
        app.router.add_get(f"{PREFIX}/login", self._login_get)
        app.router.add_post(f"{PREFIX}/login", self._login_post)
        app.router.add_get("/", self._to_index)
        app.router.add_get(f"{PREFIX}/", self._app_page)
        app.router.add_get(f"{PREFIX}/history", self._index)
        app.router.add_get(f"{PREFIX}/icon.png", self._icon)
        app.router.add_get(f"{PREFIX}/manifest.json", self._manifest)
        app.router.add_get(f"{PREFIX}/sw.js", self._sw)
        app.router.add_get(f"{PREFIX}/api/push/key", self._api_push_key)
        app.router.add_post(f"{PREFIX}/api/push", self._api_push)
        app.router.add_get(f"{PREFIX}/api/sessions", self._api_sessions)
        app.router.add_get(f"{PREFIX}/api/thread/{{num:\\d+}}", self._api_thread)
        app.router.add_post(f"{PREFIX}/api/send", self._api_send)
        app.router.add_post(f"{PREFIX}/api/settings/{{num}}", self._api_settings)
        app.router.add_post(f"{PREFIX}/api/compact/{{num:\\d+}}", self._api_compact)
        app.router.add_post(f"{PREFIX}/api/stop/{{num:\\d+}}", self._api_stop)
        app.router.add_get(PREFIX, self._to_index)
        app.router.add_get(f"{PREFIX}/t/{{tid}}", self._turn_page)
        app.router.add_get(f"{PREFIX}/t/{{tid}}/img/{{i:\\d+}}", self._turn_image)
        app.router.add_get(f"{PREFIX}/t/{{tid}}/file/{{i:\\d+}}", self._turn_file)
        app.router.add_route("*", "/{tail:.*}", self._proxy)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        log.info("小E 网页在 127.0.0.1:%d", self.port)
