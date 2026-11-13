"""对话记录：读 Claude / Codex 的历史、接力链、搜索、英文译中文、你发过的图。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import time
from pathlib import Path

from aiohttp import web

from .. import engines, thread, translate
from ..sessions import COMPACT
from ..cards import parse_decision
from ..engines.claude import NOISE
from ..folders import display
from ..report import EFFORT_CN
from ..relay import RELAY, display_items, relay_hint
from .common import _said_has, display_model, model_options

log = logging.getLogger(__name__)


class ThreadsMixin:
    def _records(self, num: int) -> list[dict]:
        out = []
        for p in self._turn_paths(num):
            try:
                r = json.loads(p.read_text())
                fs = [{"name": Path(f).name, "size": Path(f).stat().st_size if Path(f).is_file() else 0,
                       "url": f"{self.web_prefix}/dl/{r['id']}/{i}"} for i, f in enumerate(r.get("files", []))]
                out.append({"id": r["id"], "title": r.get("title", ""), "request": r.get("request", ""), "started": r.get("started"), "ended": r.get("ended"), "links": self.links_of(r), "files": fs,
                            "checkpoint_before": r.get("checkpoint_before", ""), "checkpoint_after": r.get("checkpoint_after", ""), "codex_turn_id": r.get("codex_turn_id", ""), "model": r.get("model", ""), "effort": r.get("effort", ""), "nfiles": len(r.get("changed_files") or []), "ncmds": len(r.get("commands") or []),
                            "images": [f"{self.px}/t/{r['id']}/img/{i}" for i in range(len(r.get("images", [])))],
                            "final_text": r.get("final_text", ""), "translated": r.get("translated", "")})
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return out

    def turn_records(self, num: int) -> list[dict]:
        """这个对话每轮的原始记录（旧的在前）：额度用完接力时，远窗按它拼交接材料。"""
        return [r for p in self._turn_paths(num) if (r := self._turn(p.stem))]

    def _codex_history(self, num: int) -> list[dict]:
        """Codex 对话的一来一回：Codex 的记录格式不一样，直接用远窗自己记的每轮（你说的话 + 过程 + 最后的回复）。"""
        items: list[dict] = []
        for p in self._turn_paths(num):
            try:
                r = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            tl = r.get("timeline") or []
            # 旧服务曾把接力首句截到 3000 字，原话另存于 said；恢复显示和待发比对。
            tl = [dict(e) for e in tl]
            first_you = next((e for e in tl if e.get("kind") == "you"), None)
            if (first_you and first_you.get("text", "").startswith("（这是接力的新对话")
                    and "用户这次说：\n\n" not in first_you["text"] and r.get("said")):
                first_you["text"] = r["said"]
            said = [e["text"] for e in tl if e.get("kind") == "you"] or [r.get("request") or ""]
            if any(e.get("imgs") for e in tl if e.get("kind") == "you"):   # 带图的话：图按“这一轮/第几张”从 uimg 取
                k = 0
                for e in (e for e in tl if e.get("kind") == "you"):
                    n = len(e.get("imgs") or [])
                    items.append({"who": "you", "text": "" if n and e["text"] == "（见图）" else e["text"], **({"imgs": [f"{p.stem}/{i}" for i in range(k, k + n)]} if n else {})})
                    k += n
            else:
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

    def _search_items(self, s) -> list[dict]:
        """一个对话里能搜的话（你说的、它每轮的回复），按记录文件变没变缓存住，不用每次重读几十 MB 的记录。"""
        records = engines.get(s.engine).history == "records"   # 记录在远窗这边（GPT、Kimi）还是 Claude 自己那
        if records:
            key = ("x", len(list(self.turns_dir.glob(f"{s.num}-*.json"))))
        else:
            p = thread.transcript_path(s.claude_id) if s.claude_id else None
            key = ("c", p.stat().st_mtime if p else 0)
        hit = self._search_cache.get(s.num)
        if hit and hit[0] == key:
            return hit[1]
        items: list[dict] = []
        try:
            if records:
                items = self._codex_history(s.num)
            elif s.claude_id:
                items = thread.history(thread.full_messages(s.claude_id, s.cwd), limit=100000)
        except Exception:  # noqa: BLE001 - 读不到就当没有
            log.debug("搜索读记录失败", exc_info=True)
        items = [{"who": it["who"], "text": it.get("text") or ""} for it in items if it.get("who") in ("you", "claude")]
        self._search_cache[s.num] = (key, items)
        return items

    async def _api_search(self, request: web.Request) -> web.Response:
        q = request.query.get("q", "").strip()
        b = self.bridge
        if not q:
            return web.json_response({"q": q, "title": [], "said": [], "deleted": []})
        ql, now = q.lower(), time.time()
        out: dict[str, list] = {"title": [], "said": [], "deleted": []}
        owner = request.get("owner", "admin")
        for s in sorted((x for x in b.registry.sessions.values() if x.owner == owner), key=lambda x: -x.updated):
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
        records = engines.get(s.engine).history == "records"
        lt = b.runner.last.get(num) if records and hasattr(b.runner, "last") else None
        key = (s.claude_id or s.codex_id or s.acp_id, s.updated, len(list(self.turns_dir.glob(f"{num}-*.json"))),
               lt.ended if lt else 0, self._cc_mtime(s))
        cached = self._threads.get(num)
        if cached and cached[0] == key:
            items = cached[1]
        else:
            items = []
            if records:
                items = thread.attach_records(self._codex_history(num), self._records(num))
            elif s.claude_id:
                try:
                    times: dict = {}
                    msgs = await asyncio.to_thread(thread.full_messages, s.claude_id, s.cwd, times)
                    items = thread.attach_records(thread.history(msgs, times=times), self._records(num))
                except Exception:  # noqa: BLE001 - 读不到记录就只显示进行中的
                    log.debug("读对话记录失败", exc_info=True)
            self._threads[num] = (key, items)
        return display_items(items)

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
            history.append({"who": "note", "text": "已接力 · 查看交接摘要", "handoff": following.relay_note})
        items = history + items
        for it in items:
            if it["who"] == "claude":
                it["text"] = self._zh_or_later(it["text"])
        for it in [i for i in items if i["who"] == "claude"][-2:]:   # 最近两轮的工作过程也译（再早的不译，太费）
            if it.get("process"):
                it["process"] = [self._zh_proc(e) if e["k"] in ("think", "say") else e
                                 for e in it["process"]]
        turn = b.runner.turns.get(num)
        previous_turn = getattr(b.runner, "last", {}).get(num)
        if not turn and previous_turn and previous_turn.request == RELAY and (previous_turn.is_error or previous_turn.interrupted):
            items.append({"who": "note", "text": "接力未完成，仍在原对话，可重试。"})
        if not turn and s.updated > s.seen:   # 打开了、看到最新一轮了，就不算没看
            s.seen = time.time()
            b.registry.save()
        live = None
        if turn and turn.request != RELAY:
            proc = [{"k": e["kind"], "text": str(e.get("text", "")), "detail": str(e.get("detail") or ""), "sub": e.get("sub", "")}
                    for e in turn.timeline if e["kind"] in ("think", "say", "do")]
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
        if not last and not any(it["who"] == "you" and not it.get("historical") for it in items):
            last = s.relay_pending.get("text", "")
        d = parse_decision(last, loose=True) if s.awaiting and not turn else None
        decision = {"question": d.question, "options": d.options, "recommend": d.recommend} if d else None
        recs = self._records(num)
        mid = (turn.model if turn and turn.model else "") or (recs[-1].get("model") if recs else "") or s.model or ""
        eff = s.effort or b.runner.default_effort
        pick = s.model or mid
        # 选择器展示接下来要用的型号；历史轮次仍保留那轮的实际型号。
        eng = engines.get(s.engine)
        selected = (s.model or mid) if eng.pick_model else mid   # 不选型号的（GPT）显示实际用的那个
        if not eng.pick_model and display_model(selected) in ("", "GPT"):
            selected = dict(model_options())[eng.key]
        for item in items:
            if item.get("model"):
                item["model_label"] = display_model(item["model"])
                item["effort_label"] = EFFORT_CN.get(item.get("effort"), "")
        relay_busy = bool(turn) or b.runner.busy(num)
        relaying = num in b.relays.writing
        recommendation = relay_hint(s, relay_busy, relaying, cfg=getattr(b, "cfg", None))
        if recommendation and recommendation["kind"] in ("long", "cold", "compact") and not s.relay_suggested:
            s.relay_suggested = True
            b.registry.save()
        usage = {"model": display_model(selected) if selected else "默认模型", "effort": EFFORT_CN.get(eff, eff or "默认"),
                 "mkey": eng.key if eng.pick_model != "menu" else next((k for k in ("opus", "sonnet", "haiku", "fable") if k in pick.lower()), ""),
                 "ekey": eff or "",
                 "context": round(s.context_pct) if s.context_pct is not None else None,
                 "tokens": s.context_tokens, "relay": recommendation, "relay_suggested": s.relay_suggested,
                 "compacting": b.runner.compacting(num), "busy": b.runner.busy(num)}
        queue = ["压缩上下文" if j.text == COMPACT else (t[:80] or "（图片）") for j in b.runner.queued(num) for t in (j.parts or [j.text])]
        limit = {"until": s.limit_until, "at": time.strftime("%H:%M", time.localtime(s.limit_until)), "auto": s.auto_resume} \
            if s.limit_until > time.time() and not turn else None
        five = b.runner.limits.get("five_hour")
        targets = b.relays.targets(s) if (s.claude_id or s.codex_id) and not s.relay_to and not relaying else []
        return web.json_response({"num": num, "name": chain[0].name, "cwd": display(Path(s.cwd)), "items": items,
                                  "live": live, "queue": queue, "awaiting": "" if turn else s.awaiting,
                                  "awaiting_kind": "" if turn else s.awaiting_kind, "decision": decision, "usage": usage,
                                  "limit": limit, "engine": s.engine, "ext": self._ext_data("thread_extra", s), "relay_to": s.relay_to or None,
                                  "can_relay": s.relay_suggested and bool(s.claude_id or s.codex_id) and not s.relay_to and not relay_busy and not relaying,
                                  "relay_targets": targets,
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

    def _record_image(self, num: int, ref: str) -> tuple[bytes, str] | None:
        """记录在远窗这边的对话（GPT、Kimi）里你发过的图：ref = 那一轮的编号/第几张；只认收件目录里的文件。"""
        stem, _, k = ref.partition("/")
        if not stem.startswith(f"{num}-") or not k.isdigit():
            return None
        try:
            r = json.loads((self.turns_dir / f"{stem}.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None
        paths = [p for e in r.get("timeline") or [] if e.get("kind") == "you" for p in e.get("imgs") or []]
        if int(k) >= len(paths):
            return None
        f, inbox = Path(paths[int(k)]).resolve(), (self.turns_dir.parent / "inbox").resolve()
        if not f.is_relative_to(inbox) or not f.is_file():
            return None
        return f.read_bytes(), mimetypes.guess_type(f.name)[0] or "image/png"

    async def _api_uimg(self, request: web.Request) -> web.Response:
        """对话里你发过的图。"""
        s = self.bridge.registry.get(int(request.match_info["num"]))
        ref = f"{request.match_info['uuid']}/{request.match_info['k']}"
        if not s or ref in self._uimg_miss or not (s.claude_id or engines.get(s.engine).history == "records"):
            raise web.HTTPNotFound()
        if engines.get(s.engine).history == "records":   # GPT、Kimi：图是远窗收件时存下的文件，路径记在那一轮里
            got = self._uimg.get(ref) or await asyncio.to_thread(self._record_image, s.num, ref)
        else:
            got = self._uimg.get(ref) or await asyncio.to_thread(thread.user_image, s.claude_id, s.cwd, ref)
        if not got:
            self._uimg_miss.add(ref)
            raise web.HTTPNotFound()
        self._uimg[ref] = got
        self._uimg = dict(list(self._uimg.items())[-60:])
        return web.Response(body=got[0], content_type=got[1], headers={"Cache-Control": "private, max-age=86400"})
