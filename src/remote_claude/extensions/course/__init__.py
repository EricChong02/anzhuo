"""做课扩展（私有，导出开源版时不带）：CyberScope 课程的试课预览、试课链接、课程库和试课反馈。

配置（~/.config/remote-claude/config.toml）：
  [ext.course]
  upstream = "http://127.0.0.1:8000"   # 本机 CyberScope 后端，试课预览转给它
  root = "~/…/CyberScope-courses-zy"   # 该后端 /workspace/ 对应的课程根；改了这里面的课就附“试课”链接

用起来是这样：
- 对话改到课程根里某门课的文件 → 这一轮的回复带“试课”按钮（钉钉、通知里是“试课：课名”链接）；
- 试课页由远窗转发本机 CyberScope（只放预览页、课件和预览要用的几个接口），页面上插一个“反馈这一屏”按钮；
  反馈交给做这门课的对话（没有就在课程根里新开一个），改好后回复里的按钮直接跳回反馈的那一屏；
- 侧栏顶上“课”：最近动过的两门课；课程库里能找全部的课，点名字接着做；
- 新对话“接着做《某课》”：在课程根里开，开场白说明是哪门课。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from ...ext import Api, Extension, Routes

# 试课预览放行的接口：判题、草稿课列表、提词与参考答案（后两者后端要教师登录，外网没有就自然拒绝）、心跳与前端日志
PREVIEW_API = ("/api/lesson-preview/", "/api/drafts", "/api/teacher/lecture-cues", "/api/teacher/answers",
               "/api/ping", "/api/logs", "/api/fault")
PREVIEW_PAGES = {"/studio-preview.html", "/lecture-preview.html"}
COURSE_DIRS = ("/workspace/", "/courses/")  # 课件本身（草稿在 /workspace/，上架的在 /courses/）
# CyberScope 只让“本机直连”读做课工作区（/workspace/ 课件、判题、草稿课列表），带代理来源头的一律当外来（auth.is_trusted_local）。
# 进得了这里的人已经过了小E 的登录，所以只对这三类请求去掉来源头；其余（含教师接口）照旧带着，后端照旧当外来。
FEEDBACK_PAGES = ("/studio-preview.html",)  # 试课页：转发时插一个“反馈这一屏”按钮（static/feedback.js）
LOCAL_ONLY = ("/workspace/", "/api/lesson-preview/", "/api/drafts")
PROXY_SOURCE = {"x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host", "x-forwarded-proto"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "cookie", "content-length"}
COURSE_JSON = re.compile(r"^/workspace/((?:_drafts|topics)(?:/[^/]+)*?)/([^/.][^/]*)/course\.json$")


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


def preview_url(rel: str) -> str:
    return f"/studio-preview.html?course={quote(f'/workspace/{rel}/course.json')}"


def course_previews(files: list[str], root: str) -> list[tuple[str, str]]:
    """改到的文件属于课程根里的哪几节课 → (课的目录名, 预览地址)。"""
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
                out[d.name] = preview_url(d.resolve().relative_to(base).as_posix())
                break
    return list(out.items())


def setup(api: Api, conf: dict) -> "Course | None":
    return Course(api, conf) if conf.get("root") else None


class Course(Extension):
    scripts = ("course.js",)

    def __init__(self, api: Api, conf: dict):
        super().__init__(api, conf)
        self.upstream = str(conf.get("upstream") or "http://127.0.0.1:8000").rstrip("/")
        self.root = str(conf.get("root") or "")
        self._client: aiohttp.ClientSession | None = None
        self._list: tuple[float, list] = (0.0, [])
        self._cards: tuple[float, list] = (0.0, [])
        self._migrate_feedback()

    @property
    def root_path(self) -> Path:
        return Path(self.root).expanduser()

    def _migrate_feedback(self) -> None:
        """2026-11-08 以前“最近一次试课反馈在哪一屏”存在对话的 feedback 字段，挪进扩展数据（读一次就好，存过就没了）。"""
        moved = False
        for k, v in self.api.stored().items():
            s = self.api.session(int(k))
            if s and isinstance(v, dict) and v.get("feedback") and "feedback" not in self.api.data(s):
                self.api.data(s)["feedback"] = v["feedback"]
                moved = True
        if moved:
            self.api.save()

    # ---------- 课程根里的课 ----------
    def drafts(self) -> list[dict]:
        """课程根里的课，最近改过的在前：草稿（_drafts/）和已上架的（topics/专题/）都算。
        每门课标上走到哪了：草稿 / 上架了但学生看不到 / 已发布；发布后又改过的标“有改动没发布”。15 秒算一次。"""
        at, cached = self._list
        if time.time() - at < 15:
            return cached
        root = self.root_path
        if not root.is_dir():
            return []
        try:
            pub = json.loads((root / "_published.json").read_text()).get("courses") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            pub = {}
        out = []
        for cj in [*root.glob("_drafts/*/course.json"), *root.glob("topics/*/*/course.json")]:
            d = cj.parent
            try:
                j = json.loads(cj.read_text())
            except (OSError, json.JSONDecodeError):
                j = {}
            # 课的改动多在 fragments/ 和几个主文件里，目录本身的时间不准
            mtime = 0.0
            for f in [cj, d / "cues.md", d / "教案.md", d / "index.html", d / "index.js", *d.glob("fragments/*")]:
                try:
                    mtime = max(mtime, f.stat().st_mtime)
                except OSError:
                    pass
            rel = d.relative_to(root).as_posix()
            vis = ((j.get("catalog") or {}).get("visibility") if isinstance(j, dict) else "") or ""
            p = pub.get(d.name) if isinstance(pub.get(d.name), dict) else {}
            pub_at = ((p.get("since") or {}).get("version") or 0) if p else 0
            stage = "draft" if rel.startswith("_drafts/") else "published" if vis == "published" else "hidden"
            out.append({"title": (j.get("title") if isinstance(j, dict) else "") or d.name, "dir": d.name, "rel": rel,
                        "mtime": int(mtime), "stage": stage, "pub_at": int(pub_at),
                        "dirty": bool(stage == "published" and pub_at and mtime > pub_at + 120),
                        "url": preview_url(rel)})
        out.sort(key=lambda x: x["mtime"], reverse=True)
        self._list = (time.time(), out)
        return out

    def _title(self, cdir: str) -> str:
        return next((d["title"] for d in self.drafts() if d["dir"] == cdir), cdir)

    @staticmethod
    def _manual_at(recs: list[dict], s) -> float:
        """这个对话最近一次“你亲手说话”的时间。试课页自动交来的反馈不算，
        否则反馈自己会把同一个对话越喂越新，你正在用的那个反而抢不过。"""
        return max((float(r.get("ended") or r.get("started") or 0) for r in recs
                    if not str(r.get("request") or "").startswith(("（试课反馈）", "（试课提问）"))),
                   default=float(s.created or 0))

    def course_session(self, cdir: str, title: str, owner: str = "admin"):
        """试课反馈交给谁：先看对话名和标题重合多少字；同名的对话里，交给你最近亲手在用的那个
        （试课页自动交来的反馈不算）。发过这门课试课链接的再优先一点。"""
        root = self.root_path.resolve()
        pairs = lambda t: {t[i:i + 2] for i in range(len(t) - 1)}  # 两字片段，比如“淘宝”“数据”“工程”
        want = pairs(re.sub(r"\W", "", title))
        best, best_key = None, None
        for s in self.api.sessions(owner):
            try:
                inside = Path(s.cwd).expanduser().resolve().is_relative_to(root)
            except OSError:
                inside = False
            if not inside:
                continue
            recs = self.api.records(s.num)
            linked = any(x.get("ext") == self.name and f"/{cdir}/" in x.get("url", "")
                         for r in recs for x in r.get("links", []))
            said = re.sub(r"\W", "", " ".join([s.name or "", s.last_summary or ""]))
            overlap = len(want & pairs(said)) + (5 if cdir in said else 0)
            if not linked and overlap < 3:
                continue
            key = (overlap >= 3, self._manual_at(recs, s), overlap, linked, s.updated)
            if best_key is None or key > best_key:
                best, best_key = s, key
        return best

    def account_course(self, request) -> str:
        """仅显式绑定单课且沙箱根与课程目录一致的账号可以试课。"""
        a = self.api.account(request)
        rel = a.get("course", "")
        if a.get("role") != "user" or not isinstance(rel, str):
            return ""
        if not re.fullmatch(r"(?:_drafts|topics/[^/]+)/[^/.][^/]*", rel) or ".." in rel:
            return ""
        target = (self.root_path / rel).resolve()
        if (not target.is_relative_to(self.root_path.resolve()) or
                Path(a.get("root") or "/").expanduser().resolve() != target or
                not (target / "course.json").is_file()):
            return ""
        return rel

    def allows_account_request(self, request) -> bool:
        rel = self.account_course(request)
        if not rel or any(x in request.path for x in ("..", "\\", "%")):
            return False
        path, method = request.path, request.method
        if path in (self.api.url("/api/feedback"), self.api.url("/api/course")):
            return method == ("POST" if path.endswith("feedback") else "GET")
        if method not in ("GET", "HEAD"):
            return False
        if path == "/studio-preview.html":
            return request.query.get("course") == f"/workspace/{rel}/course.json"
        if path.startswith(f"/workspace/{rel}/"):
            file = (self.root_path / path.removeprefix("/workspace/")).resolve()
            return file.is_relative_to((self.root_path / rel).resolve())
        if path.startswith(self.api.url("/static/")):
            return path in {self.api.url("/static/" + f) for f in (
                "feedback.js", "vendor/modern-screenshot.js")}
        preview_scripts = {
            "/app/lecture-reveal.js", "/app/lecture-reveal.css", "/shared/ai-availability.js", "/shared/canvas-chrome.js",
            "/shared/icons.js", "/shared/learning-result.js", "/shared/student-drafts.js",
            "/shared/xiaoxin-orb.js", "/shared/xiaoxin-presence.js", "/studio-rehearse.js",
            "/teacher/lecture-answers.js", "/teacher/lecture-cues.js",
        }
        return (path in preview_scripts or path == "/style.css" or path.startswith(
            ("/engine/", "/assets/", "/vendor/", "/courses/_shared/"))) and not path.endswith(".html")

    def _check_account_course(self, request, course):
        if self.api.account(request).get("role") == "admin":
            return
        rel = self.account_course(request)
        if not rel or course != f"/workspace/{rel}/course.json":
            raise web.HTTPForbidden(text="这个账号只开放合作课程")

    # ---------- 挂接点 ----------
    def turn_links(self, files: list[str], turn) -> list[dict]:
        return [{"label": "试课", "url": url, "title": self._title(cdir), "icon": "play", "key": cdir}
                for cdir, url in course_previews(files, self.root)]

    def upgrade_record(self, rec: dict) -> None:
        """老记录的 previews: [[课的目录名, 预览地址]] → links。"""
        if rec.get("previews") and "links" not in rec:
            rec["links"] = [{"label": "试课", "url": u, "title": self._title(n), "icon": "play", "key": n, "ext": self.name}
                            for n, u in rec["previews"]]

    def sessions_extra(self, rows: list[dict]) -> dict:
        return {"drafts": self.drafts(), "courses": self._course_cards(rows)}

    def _course_cards(self, rows: list[dict]) -> list[dict]:
        """侧栏顶上的“课”：最近动过的几门课，各自是哪个对话在做、做到哪了。30 秒算一次（要翻每个对话的记录）。"""
        at, cards = self._cards
        if time.time() - at > 30:
            cards = []
            for d in self.drafts()[:5]:
                try:
                    s = self.course_session(d["dir"], d["title"])
                except Exception:  # noqa: BLE001
                    s = None
                cards.append(dict(d, num=s.num if s else None))
            self._cards = (time.time(), cards)
        by = {r["num"]: r for r in rows}
        return [dict(c, status=by[c["num"]]["status"] if c["num"] in by else "", detail=by[c["num"]]["detail"] if c["num"] in by else "",
                     name=by[c["num"]]["name"] if c["num"] in by else "") for c in cards]

    def thread_extra(self, s) -> dict | None:
        fb = self.api.data(s).get("feedback") if s.ext.get(self.name) else None
        return {"feedback": fb} if fb else None

    async def prepare_new(self, data: dict, text: str) -> dict | None:
        """“接着做《某课》”：在课程根里开，并说明是哪门课，免得它去猜。"""
        course = str(data.get("course") or "").strip()
        c = next((d for d in self.drafts() if d["dir"] == course), None) if course else None
        if not c:
            return None
        return {"folder": str(self.root_path), "kind": "dev", "text": f"（这件事是关于课程《{c['title']}》，目录 {c['rel']}/）\n{text}"}

    def history_top(self) -> str:
        import html
        drafts = [f'<a class="btn" href="{html.escape(d["url"])}">试课：{html.escape(d["title"])}</a>' for d in self.drafts()[:6]]
        return f"<h1>最近的课</h1><p>{''.join(drafts)}</p>" if drafts else ""

    def routes(self, r: Routes) -> None:
        r.post("/api/feedback", self._api_feedback)
        r.get("/api/course", self._api_course)
        r.fallback(self._proxy)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    # ---------- 试课预览：转给本机 CyberScope ----------
    def _moved_course(self, course: str) -> str:
        """/workspace/_drafts/<课>/course.json 已不在（课上架挪进 topics/）→ 新地址；没挪就空。"""
        m = re.fullmatch(r"/workspace/_drafts/([^/]+)/course\.json", course)
        root = self.root_path
        if not m or (root / "_drafts" / m[1] / "course.json").is_file():
            return ""
        hit = next((p for p in sorted((root / "topics").glob(f"*/{m[1]}/course.json"))), None)
        return f"/workspace/{hit.relative_to(root).as_posix()}" if hit else ""

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        if not self.upstream or not preview_allowed(request.method, request.path):
            return self.api.page("不开放", "<h1>这个地址不开放</h1><p class='meta'>外网只能打开课程预览。</p>", 403)
        if self._client is None:
            self._client = aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=120))
        drop = HOP | (PROXY_SOURCE if request.path.startswith(LOCAL_ONLY) else set())
        inject = request.method == "GET" and request.path in FEEDBACK_PAGES
        if inject and (moved := self._moved_course(request.query.get("course", ""))):
            return web.HTTPFound(request.rel_url.update_query(course=moved).path_qs)  # 早先记下的草稿链接：课上架后跳到新位置
        if inject:
            drop = drop | {"accept-encoding", "if-none-match", "if-modified-since"}  # 要改页面，取不压缩、完整的一份
        headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        try:
            async with self._client.request(request.method, self.upstream + request.rel_url.path_qs,
                                            headers=headers, data=await request.read(), allow_redirects=False) as up:
                if inject and up.status == 200 and "text/html" in up.headers.get("Content-Type", ""):
                    page = (await up.read()).decode("utf-8", "replace")
                    tag = f'<script src="{self.api.url("/static/feedback.js")}"></script>'
                    page = page.replace("</body>", tag + "</body>", 1) if "</body>" in page else page + tag
                    return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})
                resp = web.StreamResponse(status=up.status, headers={
                    k: v for k, v in up.headers.items() if k.lower() not in HOP | {"set-cookie"}})
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(64 * 1024):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except aiohttp.ClientError:
            return self.api.page("预览没开", "<h1>本机的课程预览没开</h1><p class='meta'>Mac 上的 CyberScope（8000 端口）"
                                 "没在运行。在钉钉里让远窗帮你启动，或回到电脑前双击“做课.command”。</p>", 502)

    # ---------- 试课反馈 ----------
    async def _api_feedback(self, request: web.Request) -> web.Response:
        """试课页上“反馈这一屏”：带上课、节、屏，交给做这门课的对话；没有就在课程根里新开一个。"""
        if not self.api.same_origin(request):
            raise web.HTTPForbidden()
        data = await request.json()
        # 课可能放在专题子文件夹里：topics/bx1/data-l02/course.json，取最后一层当课名，中间的算进路径
        m = COURSE_JSON.match(str(data.get("course") or ""))
        if not m or ".." in m.group(1).split("/"):
            return web.json_response({"ok": False, "error": "认不出是哪门课"}, status=400)
        self._check_account_course(request, str(data.get("course") or ""))
        cdir = m.group(2)
        title = self._title(cdir)
        if request.get("owner", "admin") != "admin":
            return await self._account_feedback(request, data, m, title)
        # 一次可以带几处（试课页上“先记下”攒的），每处：节、屏、说了什么、截图
        notes = [x for x in (data.get("items") or [data]) if isinstance(x, dict)][:12]
        lines, images = [], []
        for x in notes:
            where = self._where(m.group(1), cdir, x)
            text = str(x.get("text") or "").strip()
            imgs = await self.api.save_images((x.get("images") or [])[:max(0, 6 - len(images))])
            images += imgs
            lines.append((where, x.get("section") or "未知", (text or "见截图") + (f"（附图 {len(imgs)} 张）" if imgs and len(notes) > 1 else "")))
        if data.get("ask") and lines:
            where, sec, text = lines[-1]
            q = text if text != "见截图" else "这一屏是想让学生做什么、学到什么？在这一节里起什么作用？"
            msg = f"（试课提问）《{title}》{where}（{m.group(1)}/{cdir}，节 id {sec}）：\n{q}\n\n先别改，对照教案用几句话说清楚就行。"
        elif len(lines) == 1:
            where, sec, text = lines[0]
            msg = f"（试课反馈）《{title}》{where}（{m.group(1)}/{cdir}，节 id {sec}）：\n{text}\n\n改完截图给我看这一屏。"
        else:
            msg = (f"（试课反馈）《{title}》共 {len(lines)} 处（{m.group(1)}/{cdir}）：\n"
                   + "\n".join(f"{i}. {w}（节 id {sec}）：{t}" for i, (w, sec, t) in enumerate(lines, 1))
                   + "\n\n一处一处改，改完把改过的几屏截图给我看。")
        last = notes[-1] if notes else {}
        self.api.record("你（试课提问）" if data.get("ask") else "你（试课反馈）", msg, images=len(images))
        spot = {"course": str(data.get("course")), "section": last.get("section") or "", "screen": last.get("screen"), "step": last.get("step"),
                "where": self._where(m.group(1), cdir, last), "t": time.time()}
        s = self.course_session(cdir, title)
        if s:
            self.api.data(s)["feedback"] = spot   # 记下最后一处在哪：改好后回复里的试课链接直接跳回这一屏
            await self.api.send(s.num, msg, images, via="feedback")
            return web.json_response({"ok": True, "num": s.num, "name": s.name})
        s = await self.api.new(str(self.root_path), msg, images, kind="dev", note="（试课反馈新开）", via="feedback")
        if s:
            self.api.data(s)["feedback"] = spot
            self.api.save()
        return web.json_response({"ok": bool(s), "num": s.num if s else None, "name": s.name if s else "新对话"})

    async def _account_feedback(self, request, data, m, title):
        owner = request["owner"]
        notes = [x for x in (data.get("items") or [data]) if isinstance(x, dict)][:12]
        if not notes:
            return web.json_response({"ok": False, "error": "没写反馈"}, status=400)
        lines, images = [], []
        for x in notes:
            where = self._where(m.group(1), m.group(2), x)
            lines.append(f"{where}（节 id {x.get('section') or '未知'}）：{str(x.get('text') or '见截图')[:8000]}")
            images.extend((x.get("images") or [])[:max(0, 6-len(images))])
        ask = bool(data.get("ask"))
        msg = (f"（试课{'提问' if ask else '反馈'}）《{title}》（{m.group(1)}/{m.group(2)}）：\n"
               + "\n".join(lines) + ("\n先别改，对照本课教案解释。" if ask else
               "\n请直接修改本课，保留其他内容。完成后说明修改了哪一屏，提醒我刷新该屏核对。只在本课目录内工作。"))
        s = self.course_session(m.group(2), title, owner)
        response = await self.api.send_fenced(request, msg, images, s.num if s else None)
        result = json.loads(response.text)
        if not result.get("ok"):
            return response
        s = s or self.api.session(result.get("num"))
        if s:
            last = notes[-1]
            self.api.data(s)["feedback"] = {"course": data["course"], "section": last.get("section") or "",
                "screen": last.get("screen"), "step": last.get("step"),
                "where": self._where(m.group(1), m.group(2), last), "t": time.time()}
            self.api.save()
        return web.json_response({"ok": bool(s), "num": s.num if s else None, "name": s.name if s else ""})

    def _where(self, parent: str, cdir: str, x: dict) -> str:
        """“第 2 节「数据从哪来」第 3 屏”：页面没读到节名时，自己从课程文件里查。"""
        sec_no, sec_title, screen = x.get("section_no") or 0, str(x.get("section_title") or ""), x.get("screen")
        if not sec_title and x.get("section"):
            try:
                secs = json.loads((self.root_path / parent / cdir / "course.json").read_text()).get("sections") or []
                k = next(i for i, y in enumerate(secs) if y.get("id") == x["section"])
                sec_no, sec_title = k + 1, secs[k].get("title") or ""
            except (OSError, ValueError, StopIteration, AttributeError):
                pass
        # “老师讲课”模式下停在这一屏的第几步（封面 / 进屏 / 第 k 步，和 cues.md 的叫法一致）；学生视角整屏全显示，不带
        step, steps = x.get("step"), x.get("steps")
        name = ""
        if isinstance(screen, int) and screen >= 0 and isinstance(step, int) and isinstance(steps, int) and 0 <= step <= steps and steps > 0:
            name = str(x.get("step_name") or "")[:8] if step == 0 else f"第 {step} 步"
            name = f"的{name or '进屏'}（共 {steps} 步）"
        return "".join([f"第 {sec_no} 节「{sec_title}」" if sec_no else "",
                        f"第 {int(screen) + 1} 屏{name}" if isinstance(screen, int) and screen >= 0 else ""])

    async def _api_course(self, request: web.Request) -> web.Response:
        """试课页右下角的小条、课程库点名字：这门课是哪个对话在改、改到哪了、改完没有。"""
        self._check_account_course(request, request.query.get("course", ""))
        m = COURSE_JSON.match(request.query.get("course", ""))
        if not m:
            return web.json_response({"num": None})
        cdir = m.group(2)
        s = await asyncio.to_thread(self.course_session, cdir, self._title(cdir), request.get("owner", "admin"))
        if not s:
            return web.json_response({"num": None})
        st = self.api.status(s.num)
        return web.json_response({"num": s.num, "name": s.name, "state": st["state"], "step": st["step"],
                                  "minutes": st["minutes"], "updated": s.updated,
                                  "feedback": self.api.data(s).get("feedback") if s.ext.get(self.name) else None})
