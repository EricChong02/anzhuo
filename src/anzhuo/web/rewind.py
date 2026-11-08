"""回退与分叉：退回到某条回复之前（Claude 截记录、Codex 回滚轮次并恢复文件），或从那里分出新对话。"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

from .. import engines, thread
from ..sessions import Session

log = logging.getLogger(__name__)


class RewindMixin:
    async def _codex_target(self, s, to: str) -> str:
        records = self._records(s.num)
        rec = next((r for r in records if r["id"] == to), None)
        if not rec or not s.codex_id:
            raise ValueError("找不到那条回复，原对话未改动")
        if rec.get("codex_turn_id"):
            return rec["codex_turn_id"]
        # 老记录没有原生轮次编号时，只按唯一、接近的开始时间核验，不猜序号。
        from ..codex import CodexConn
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

    async def _api_fork(self, request: web.Request) -> web.Response:
        """分叉：从某条回复（或你某句话之前）另开一个对话，带着到那里为止的来龙去脉，原对话不动。
        {"to": 那条回复的记录编号}；空 = 从头另开（你第一句话之前，什么都不带）。文件不复制，两边改的是同一个文件夹。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        p = b.registry.get(int(request.match_info["num"]))
        if not p:
            raise web.HTTPNotFound()
        eng = engines.get(p.engine)
        if not eng.can_fork:
            return web.json_response({"ok": False, "error": f"{eng.label} 的对话暂不支持分叉"})
        to = str((await request.json()).get("to") or "")
        if eng.history == "records":   # GPT：按远窗的每轮记录找轮次，下一句时 thread/fork 出新分支
            try:
                cut = await self._codex_target(p, to) if to else ""
            except (ValueError, RuntimeError) as e:
                return web.json_response({"ok": False, "error": str(e)})
            records = self._records(p.num)
            ids = [r["id"] for r in records]
            kept = ids[:ids.index(to) + 1] if to else []
            now = time.time()
            s = Session(num=b.registry.reserve_num(), name=p.name[:8] + "·分叉", cwd=p.cwd, owner=p.owner, root=p.root,
                        add_dirs=list(p.add_dirs), engine=p.engine, model=p.model, effort=p.effort,
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
        eng = engines.get(s.engine)
        if not eng.can_rewind:
            return web.json_response({"ok": False, "error": f"{eng.label} 的对话暂不支持回退"})
        data = await request.json()
        if data.get("cancel"):
            if s.forking:
                return web.json_response({"ok": False, "error": "分叉出来的对话不能撤销，不要了就删掉它"})
            if eng.history == "records":
                if s.codex_file_undo:
                    try:
                        self._file_restore_idle(s)
                        from ..checkpoints import Checkpoints
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
        if eng.history == "records":   # GPT：截记录、切分支、按快照退文件
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
            why = await engines.get(s.engine).rewind_files(s, later[0]["uuid"])
            note += " · " + (why or "文件已退回")
        s.rewind_at, s.rewind_note = to, note
        kept = items[k]["text"]
        from ..intent import asks_user, awaiting_kind
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
                from ..checkpoints import Checkpoints
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
