"""GPT 引擎：ChatGPT 自带的 Codex（常驻连接 app-server，见 anzhuo/codex.py）。
事件翻成和 Claude 一样的步骤、说明、回复；中途插话用 turn/steer；跑命令要批准时走 policy；每轮前后记文件快照（回退用）。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import codex as codex_conn
from ..cards import step_label
from ..checkpoints import Checkpoints
from ..report import brief
from . import policy
from .base import COMPACT, Engine, check_folders, codex_instructions, handoff, role

if TYPE_CHECKING:
    from ..report import Turn
    from ..sessions import Job, Runner, Session

log = logging.getLogger(__name__)

COMMAND_APPROVALS = ("item/commandExecution/requestApproval", "execCommandApproval")


async def approve_command(runner: "Runner", s: "Session", turn: "Turn", method: str, prm: dict, stopped: bool = False) -> dict:
    """Codex 跑命令前来问：危险的交给 policy 问人，其余直接放行（只能写这个文件夹，本来就出不去）。
    没带命令的（往已开的终端里写输入之类）照旧放行。stopped：原任务已停，一律不批。对话的命令批准和生图委派共用。"""
    cmd = prm.get("command") or ""
    cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    ok = not stopped and await policy.approve(runner, s, turn, policy.EXECUTE, cmd) is not False
    if method == "execCommandApproval":
        return {"decision": "approved" if ok else "denied"}
    return {"decision": "accept" if ok else "decline"}


class CodexEngine(Engine):
    name, label, tool, key = "codex", "GPT", "Codex", "gpt"
    pick_model = "own"   # 型号用 Codex 自己报的（model/list）；不选就用 ~/.codex/config.toml 里的
    can_steer = can_rewind = can_fork = has_quota = has_tools = stop_hook = True

    def available(self) -> bool:
        return codex_conn.codex_bin() is not None

    def models(self) -> tuple[list[str], str]:
        return list(codex_conn.MODELS_SEEN), codex_conn.configured_model() or "Codex 自己配置的模型"

    def limits(self, runner: "Runner") -> dict:
        return runner.limits_gpt

    async def refresh_quota(self, runner: "Runner", force: bool = False) -> None:
        await (runner.refresh_gpt_usage(force=True) if force else runner.refresh_gpt_usage())

    async def halt(self, conn: Any) -> None:
        try:
            await conn.interrupt()
        except Exception:  # noqa: BLE001 - 停不下来就直接关掉
            await conn.close()

    def steer(self, runner, s, turn, job):
        # 只在连接还开着时插（turn/steer）；没连上就排到下一轮
        return self._steer(runner, s, job) if s.num in runner.live else None

    async def _steer(self, runner: "Runner", s: "Session", job: "Job") -> None:
        try:
            await runner.live[s.num].steer(job.text, job.images)
            job.finished = True
        except Exception:  # noqa: BLE001 - 这一轮刚好结束、插不进去：排到下一轮
            log.warning("插话给 Codex #%s 失败，排到下一轮", s.num, exc_info=True)
            runner.requeue(s.num, job)

    async def run(self, runner: "Runner", s: "Session", job: "Job", turn: "Turn") -> None:
        """开或接着会话 → 开一轮 → 把事件翻成和 Claude 一样的步骤、说明、回复；正在写的回复边写边给网页。
        只能写对话所在的文件夹（workspace-write，可联网）。"""
        await check_folders(s)
        checkpoints = Checkpoints(runner.registry.path.parent / "checkpoints")
        if job.text != COMPACT:
            try:
                turn.checkpoint_before = await asyncio.to_thread(checkpoints.capture, [s.cwd, *s.add_dirs])
            except OSError:
                log.warning("#%s 文件快照未建立", s.num)
        effort = s.effort or runner.default_effort or "medium"
        texts: list[str] = []
        started_cmds: set[str] = set()

        def on_event(method: str, prm: dict) -> None:
            turn.last_at = time.time()
            item = prm.get("item") or {}
            kind = item.get("type")
            if method == "item/agentMessage/delta":
                turn.partial = (turn.partial + str(prm.get("delta") or ""))[-6000:]
            elif method == "item/started" and kind == "commandExecution":
                cmd = str(item.get("command") or "")
                cmd = re.sub(r"^/bin/(?:zsh|bash) -lc ", "", cmd).strip("'\"")
                started_cmds.add(item.get("id") or cmd)
                label = "跑命令：" + brief(cmd, 18)
                turn.steps.append(label)
                turn.commands.append(cmd[:200])
                turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": cmd[:3000]})
            elif method == "item/completed" and kind == "agentMessage" and (item.get("text") or "").strip():
                turn.partial = ""
                texts.append(item["text"].strip())
                turn.timeline.append({"t": time.time(), "kind": "say", "text": item["text"][:6000]})
            elif method == "item/completed" and kind == "reasoning":
                summ = item.get("summary") or []
                text = "\n".join(x if isinstance(x, str) else str(x.get("text", "")) for x in summ).strip()
                if text:
                    turn.timeline.append({"t": time.time(), "kind": "think", "text": text[:4000]})
            elif method == "item/completed" and kind == "fileChange":
                for ch in item.get("changes") or []:
                    fp = ch.get("path") if isinstance(ch, dict) else None
                    if fp:
                        turn.changed_files.append(fp)
                        label = f"改 {Path(fp).name}"
                        turn.steps.append(label)
                        turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": fp})
            elif method == "item/completed" and kind == "webSearch":
                q = str(item.get("query") or "")
                label = "上网查：" + brief(q, 18)
                turn.steps.append(label)
                turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": q})
            elif method == "turn/plan/updated":   # Codex 自己列的步骤清单
                st = {"completed": "done", "inProgress": "doing", "in_progress": "doing"}
                turn.plan = [{"text": str(x.get("step", ""))[:60], "status": st.get(x.get("status"), "pending")} for x in (prm.get("plan") or [])[:20]]
            elif method == "thread/tokenUsage/updated":
                u = prm.get("tokenUsage") or {}
                if total := (u.get("total") or {}).get("totalTokens"):
                    tot = u["total"]
                    turn.usage = {"codex": {"input": int(tot.get("inputTokens") or 0), "cached": int(tot.get("cachedInputTokens") or 0),
                                            "output": int(tot.get("outputTokens") or 0), "total": int(total)}}
                win, last = u.get("modelContextWindow"), (u.get("last") or {}).get("totalTokens")
                if win and last:
                    turn.context_pct = s.context_pct = min(100.0, 100.0 * last / win)
                    s.context_tokens = int(last)
            elif method == "account/rateLimits/updated":
                if got := codex_conn.limits_from(prm.get("rateLimits") or {}):
                    runner.limits_gpt = got
            elif method == "error":
                msg = str((prm.get("error") or {}).get("message") or prm.get("message") or "")
                if msg and not prm.get("willRetry"):
                    turn.error = msg[:300]

        async def on_request(method: str, prm: dict) -> dict | None:
            if method in COMMAND_APPROVALS:
                return await approve_command(runner, s, turn, method, prm)
            if method == "item/fileChange/requestApproval":
                return {"decision": "accept"}
            if method == "applyPatchApproval":
                return {"decision": "approved"}
            if method == "item/tool/call" and (h := handlers.get(str(prm.get("tool") or ""))):   # 远窗给它的工具（总管派活的汇报、退回）
                args = prm.get("arguments") if isinstance(prm.get("arguments"), dict) else {}
                if label := step_label(f"mcp__anzhuo__{prm.get('tool')}", args):
                    turn.steps.append(label)
                    turn.timeline.append({"t": time.time(), "kind": "do", "text": label, "detail": json.dumps(args, ensure_ascii=False)[:500]})
                res = await h(args)
                text = "".join(str(c.get("text") or "") for c in res.get("content") or [])
                return {"contentItems": [{"type": "inputText", "text": text}], "success": not res.get("isError")}
            return None

        # 特殊角色（总管派出去的对话）的工具和约定：Codex 用 dynamicTools 交给它（只在新开会话时能带上）
        rl = role(runner, s)
        handlers = {t.name: t.handler for t in rl.get("tools") or []}
        specs = [{"type": "function", "name": t.name, "description": t.description, "inputSchema": t.input_schema}
                 for t in rl.get("tools") or []]
        conn = codex_conn.CodexConn(s.cwd, on_event, on_request, **({"tools": specs} if specs else {}))   # 没有工具时调用和原来一样
        runner.live[s.num] = conn
        turn.model = "GPT（Codex）"
        turn.timeline.append({"t": time.time(), "kind": "you", "text": (job.text or "（见图）"),
                              **({"imgs": [str(p) for p in job.images]} if job.images else {})})   # 记下图在哪，网页上才能把你发的图再显示出来
        try:
            await conn.start()
            if s.rewind_at:
                if not s.codex_id or not s.codex_cut_turn:
                    raise codex_conn.CodexError("找不到回退位置，原对话未改动")
                new_id = await conn.fork_thread(s.codex_id, s.codex_cut_turn)
                s.codex_id = new_id
                s.rewind_at = s.rewind_note = s.codex_cut_turn = ""
                s.forking = False
                s.codex_pending_hidden = []
                s.codex_file_undo = ""
                s.context_pct = s.context_tokens = None
                runner.registry.save()
            else:
                s.codex_id = await conn.open_thread(s.codex_id, codex_instructions(runner.language, runner.extra_instructions) + rl.get("append", ""),
                                                    s.model or None)
            turn.model = f"GPT（{conn.model}）" if conn.model else turn.model
            if job.text == COMPACT:   # 压缩上下文
                before = s.context_pct
                await conn.compact()
                await asyncio.wait_for(conn.done.wait(), 600)
                conn.turn_status = conn.turn_status or "completed"
                turn.final_text = "压缩好了。"
                if before is not None:
                    s.context_pct = turn.context_pct = s.context_tokens = None   # 压完的用量等下一轮再报
            else:
                turn.codex_turn_id = await conn.start_turn(job.text, job.images, effort, s.model or None)
                await conn.done.wait()
                # Codex 没有 Stop 钩子：做完再看一眼（unfinished），该汇报没汇报就接着补一轮；回复仍用补之前那段
                if conn.turn_status == "completed" and not turn.interrupted and (why := self.unfinished(runner, s, turn, False)):
                    kept = list(texts)
                    conn.done.clear()
                    conn.turn_status = ""
                    turn.codex_turn_id = await conn.start_turn(why, [], effort, s.model or None)
                    await conn.done.wait()
                    if kept:
                        texts[:] = kept
        except codex_conn.CodexError as e:
            turn.is_error = True
            turn.error = str(e)[:300]
        finally:
            turn.accepting = False
            runner.live.pop(s.num, None)
            await conn.close()
            if turn.checkpoint_before:
                try:
                    turn.checkpoint_after = await asyncio.to_thread(checkpoints.capture, [s.cwd, *s.add_dirs])
                except OSError:
                    log.warning("#%s 文件结束快照未建立", s.num)
        if conn.turn_status == "failed" and not turn.interrupted:
            turn.is_error = True
            turn.error = (conn.turn_error or turn.error or "Codex 这一轮失败了")[:300]
        elif not conn.turn_status and not turn.is_error and not turn.interrupted:
            turn.is_error = True
            turn.error = ("Codex 中途退出了：" + conn.stderr_tail())[:300]
        turn.partial = ""
        if job.text != COMPACT:
            turn.final_text = texts[-1] if texts else ""
        turn.final_text, imgs, files = handoff(turn.final_text, s.cwd)
        turn.images += imgs
        turn.files += files
        turn.effort = effort
