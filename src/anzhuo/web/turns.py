"""每一轮的“完整过程”页：存档、过程页、图片和文件下载、PDF 分页预览、历史列表。"""
from __future__ import annotations

import asyncio
import shutil
import html
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from ..folders import display
from .common import _md

log = logging.getLogger(__name__)


class TurnsMixin:
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
            "id": tid, "label": label, "title": title, "request": turn.request, "said": turn.said, "cwd": cwd,
            "started": turn.started, "ended": turn.ended, "schedule_id": turn.schedule_id, "schedule_run_id": turn.schedule_run_id, "checkpoint_before": turn.checkpoint_before, "checkpoint_after": turn.checkpoint_after, "codex_turn_id": turn.codex_turn_id, "model": turn.model, "effort": turn.effort,
            "final_text": turn.final_text, "error": turn.error, "timeline": turn.timeline,
            "changed_files": files, "commands": turn.commands, "images": kept_images, "files": kept_files,
            "links": self._turn_links(files, turn), "translated": turn.translated,
        }
        self.turns_dir.mkdir(parents=True, exist_ok=True)
        (self.turns_dir / f"{tid}.json").write_text(json.dumps(rec, ensure_ascii=False))
        return tid

    def _turn_links(self, files: list[str], turn) -> list[dict]:
        out = []
        for e in self.exts:
            try:
                out += [dict(x, ext=e.name) for x in e.turn_links(files, turn) or []]
            except Exception:  # noqa: BLE001 - 扩展出错不影响记录
                log.exception("扩展 %s 算链接出错", e.name)
        return out

    def links_of(self, rec: dict) -> list[dict]:
        """这一轮回复附的链接 [{label, url, title, icon, key, ext}]；老样子的记录先让扩展改成现在的样子。"""
        for e in self.exts:
            try:
                e.upgrade_record(rec)
            except Exception:  # noqa: BLE001
                log.exception("扩展 %s 读老记录出错", e.name)
        return [x for x in rec.get("links") or [] if isinstance(x, dict) and x.get("url")]

    def _ext_data(self, hook: str, *args) -> dict:
        """各扩展附在接口里的数据：{扩展名: 数据}，没有的不放。"""
        out = {}
        for e in self.exts:
            try:
                if (got := getattr(e, hook)(*args)) is not None:
                    out[e.name] = got
            except Exception:  # noqa: BLE001
                log.exception("扩展 %s 的 %s 出错", e.name, hook)
        return out

    def _turn(self, tid: str) -> dict | None:
        if not re.fullmatch(r"\d+-\d+", tid):
            return None
        try:
            return json.loads((self.turns_dir / f"{tid}.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

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
            if s and s.owner != "admin":
                continue
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
        top = "".join(self._ext_data("history_top").values())
        return self._page("远窗 · 历史记录", top + "<h1>历史记录</h1><p class='meta'>按对话列，最近动过的在前；点名字回到那个对话。</p>"
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
        if links := self.links_of(r):
            out.append("<p>" + "".join(f'<a class="btn" href="{html.escape(x["url"])}">{html.escape(x.get("label") or "打开")}'
                                       + (f'：{html.escape(x["title"])}' if x.get("title") else "") + "</a>" for x in links) + "</p>")
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
