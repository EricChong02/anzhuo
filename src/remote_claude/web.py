"""小E 网页：完整过程页和试课预览。只听本机端口，经 SSH 反向隧道挂到阿里云（如 https://xiaoe.ericlab.cn）。

进门靠钉钉里发的一次性链接：点开换一个 30 天的登录凭证（Cookie），链接随即作废；同一浏览器再点旧链接仍能进。
试课预览只转本机 CyberScope 的课程预览页、静态文件和预览要用的几个接口；教师台、学生数据、WebSocket 一律不转。
"""
from __future__ import annotations

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

from .folders import display

log = logging.getLogger(__name__)

COOKIE = "xiaoe_sid"
SESSION_DAYS = 30
LINK_DAYS = 7
PREFIX = "/_x"  # 小E 自己的页面都在这下面，其余路径留给试课预览

# 试课预览放行的接口：判题、草稿课列表、提词与参考答案（后两者后端要教师登录，外网没有就自然拒绝）、心跳与前端日志
PREVIEW_API = ("/api/lesson-preview/", "/api/drafts", "/api/teacher/lecture-cues", "/api/teacher/answers",
               "/api/ping", "/api/logs", "/api/fault")
PREVIEW_PAGES = {"/studio-preview.html", "/lecture-preview.html"}
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
        return path in PREVIEW_PAGES
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
    def __init__(self, base: str, port: int, data_dir: Path, preview_upstream: str = "", preview_root: str = ""):
        self.base = base.rstrip("/")
        self.port = port
        self.preview_upstream = preview_upstream.rstrip("/")
        self.preview_root = preview_root
        self.turns_dir = data_dir / "turns"
        self.store_path = data_dir / "web.json"
        self.store = self._load()
        self._client: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    # ---------- 一次性链接与登录 ----------
    def _load(self) -> dict:
        try:
            d = json.loads(self.store_path.read_text())
        except (OSError, json.JSONDecodeError):
            d = {}
        return {"links": d.get("links", {}), "sessions": d.get("sessions", {})}

    def _save(self) -> None:
        now = time.time()
        self.store["links"] = {k: v for k, v in self.store["links"].items() if v["exp"] > now}
        self.store["sessions"] = {k: v for k, v in self.store["sessions"].items() if v > now}
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.store))
        tmp.chmod(0o600)
        tmp.replace(self.store_path)

    def link(self, target: str) -> str:
        """给某个页面发一个一次性进门链接（7 天内有效）。"""
        token = secrets.token_urlsafe(24)
        self.store["links"][token] = {"target": target, "exp": time.time() + LINK_DAYS * 86400, "used": False}
        self._save()
        return f"{self.base}{PREFIX}/l/{token}"

    def _logged_in(self, request: web.Request) -> bool:
        sid = request.cookies.get(COOKIE, "")
        return bool(sid) and self.store["sessions"].get(sid, 0) > time.time()

    # ---------- 记下一轮的完整过程 ----------
    def save_turn(self, label: str, turn, cwd: str, title: str) -> str:
        tid = f"{label.split()[0].lstrip('#')}-{int(turn.started)}"
        files = list(dict.fromkeys(turn.changed_files))
        rec = {
            "id": tid, "label": label, "title": title, "request": turn.request, "cwd": cwd,
            "started": turn.started, "ended": turn.ended, "model": turn.model, "effort": turn.effort,
            "final_text": turn.final_text, "error": turn.error, "timeline": turn.timeline,
            "changed_files": files, "commands": turn.commands, "images": turn.images,
            "previews": course_previews(files, self.preview_root),
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

    def _denied(self) -> web.Response:
        return self._page("需要登录", "<h1>需要先登录</h1><p class='meta'>链接过期或已经用过了。"
                          "在钉钉里给小E 发“网页”，会收到一个新的进门链接。</p>", 403)

    async def _login(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        link = self.store["links"].get(token)
        if not link or link["exp"] < time.time():
            return self._denied()
        if link["used"]:
            if self._logged_in(request):
                raise web.HTTPFound(link["target"])
            return self._denied()
        link["used"] = True
        sid = secrets.token_urlsafe(32)
        self.store["sessions"][sid] = time.time() + SESSION_DAYS * 86400
        self._save()
        resp = web.HTTPFound(link["target"])
        resp.set_cookie(COOKIE, sid, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax",
                        secure=self.base.startswith("https://"), path="/")
        raise resp

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
        return self._page("小E · 最近的活", "<h1>最近的活</h1>" + ("".join(rows) or "<p class='meta'>还没有记录。</p>"))

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

    # ---------- 试课预览：转给本机 CyberScope ----------
    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        if not self.preview_upstream or not preview_allowed(request.method, request.path):
            return self._page("不开放", "<h1>这个地址不开放</h1><p class='meta'>外网只能打开课程预览。</p>", 403)
        if self._client is None:
            self._client = aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=120))
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
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

    # ---------- 启动 ----------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.path.startswith(f"{PREFIX}/l/"):
            return await handler(request)
        if not self._logged_in(request):
            return self._denied()
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard], client_max_size=20 * 1024 * 1024)
        app.router.add_get(f"{PREFIX}/l/{{token}}", self._login)
        app.router.add_get(f"{PREFIX}/", self._index)
        app.router.add_get(PREFIX, self._to_index)
        app.router.add_get(f"{PREFIX}/t/{{tid}}", self._turn_page)
        app.router.add_get(f"{PREFIX}/t/{{tid}}/img/{{i:\\d+}}", self._turn_image)
        app.router.add_route("*", "/{tail:.*}", self._proxy)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        log.info("小E 网页在 127.0.0.1:%d", self.port)
