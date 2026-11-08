"""Claude 引擎：claude-agent-sdk 的 ClaudeSDKClient。PreToolUse 钩子拦危险命令（走 policy）、受限账号的护栏、
总管不亲自干活的拒绝钩子、Stop 钩子（角色的 on_stop）；进程内 MCP 工具交图、交文件、列步骤、委派生图。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage, StreamEvent, SystemMessage, TextBlock,
    ThinkingBlock, ToolUseBlock, create_sdk_mcp_server, tool,
)
from claude_agent_sdk.types import TaskNotificationMessage, TaskProgressMessage, TaskStartedMessage, TaskUpdatedMessage

from ..cards import step_label
from ..folders import display
from . import policy
from .base import CONFIRM_WAIT, Engine, check_folders, role

if TYPE_CHECKING:
    from ..report import Turn
    from ..sessions import Job, Runner, Session

log = logging.getLogger(__name__)

# 给每个对话追加的固定要求。这几条是远窗与对话之间的“约定”（交图、交文件、列步骤、“要你定”的写法），
# 网页和手机上的按钮、进度条都靠它们工作，所以内置；你自己的习惯和授权写进配置的 extra_instructions，不要改这里。
RULES = """需要AI生图或改图时，调用 generate_images 工具委派 Codex，传清楚提示词、参考图绝对路径及输出目录；不要用占位图冒充生成图。工具成功会自动登记图片附件，不要重复调用 send_images；仍需检查画面是否符合要求；失败应如实说明，不声称已经发出。
做了截图、或有能说明结果的图片时，调用 send_images 工具把图片的绝对路径交出去（最多 6 张）。
需要用户在几个方案里选一个时，先把各方案做成截图并用 send_images 交出，回复第一行写“要你定：”加一句问题，然后列出选项和你的推荐，停下等回复。
选项每个单独一行，写成“A. 一句话说明”“B. …”（最多 4 个）；推荐单独一行，写成“推荐：A，理由”。
用户要文件（PDF、文档、表格、原件等）时，用 send_files 把文件原样交给用户，不要只发截图；截图只用于看效果。
动手做事、且要多次操作才能完成时（改几处文件、查资料再改、做完再验证等），第一个动作先调用 mcp__anzhuo__update_plan 列出步骤（3—8 步，每步一句 12 字以内的人话，第一步标 doing），之后每开始或做完一步就再调一次更新状态，最后全部标 done 再写回复。用户人在外面，只能靠这份清单知道你做到哪了。只聊天、一两条命令就能答完的不用列。
这一轮回复发出后，本轮里开的后台命令（run_in_background）和 Monitor 都会被停掉，也收不到它们的完成通知。要跑得比这一轮长的活（上线、完整验收、长时间生成），用 nohup … > 日志文件 2>&1 & 脱离启动，回复里写清大约多久、用户下次问时你去看哪个日志；不要说“我盯着、完成后告诉你”。
"""


def build_instructions(language: str = "简体中文", extra: str = "") -> str:
    """内置约定 + 回复语言 + 你自己的追加要求（配置里的 reply_language、extra_instructions）。"""
    lang = f"回复一律用{language}，思考和过程中的说明也用{language}；专有名词、代码、命令、路径保持原样。\n" if language else ""
    return lang + RULES + (extra.strip() + "\n" if extra.strip() else "")


def build_user_instructions(root: str, language: str = "简体中文", extra: str = "") -> str:
    """受限账号（role = user）的对话：只能在自己的文件夹里干活。不给生图委派（Codex 在沙箱外跑），不提远窗自己怎么重启。"""
    text = build_instructions(language, extra)
    return "".join(ln + "\n" for ln in text.splitlines() if "generate_images" not in ln and "anzhuo" not in ln) + (
        f"只在当前工作文件夹（{root}）里工作：读、写、运行命令都限于这个文件夹，不访问它外面的文件。\n")


# 受限对话能用的工具；别的（定时、远程触发、工作树、Monitor 等）一律拦下


USER_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS", "Bash", "BashOutput", "KillShell",
              "KillBash", "TaskOutput", "TaskStop", "WebFetch", "WebSearch", "TodoWrite", "Task", "Agent", "ExitPlanMode",
              "AskUserQuestion", "ToolSearch", "mcp__anzhuo__send_images", "mcp__anzhuo__send_files", "mcp__anzhuo__update_plan"}
PATH_KEYS = ("file_path", "notebook_path", "path")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
AGENT_WAIT = 20 * 60   # 主对话说完后，最多再等后台助手这么久
DONE = {"completed", "failed", "stopped", "killed", "error", "cancelled"}
NOISE = re.compile(r"^\s*(No response requested\.?)?\s*$", re.I)   # 空结果也算：后台通知有时连一句话都不答就出结果


class ClaudeEngine(Engine):
    name, label, key = "claude", "Claude", "claude"
    pick_model = "menu"
    history = "transcript"
    can_steer = can_rewind = can_fork = has_quota = has_tools = stop_hook = fenced_ok = cache_hint = cum_usage = True
    context_window: int | None = None   # None = 上下文百分比照 Claude Code 报的；它不认识的后端（DeepSeek）要自己给窗口大小

    def limits(self, runner: "Runner") -> dict:
        return runner.limits

    async def refresh_quota(self, runner: "Runner", force: bool = False) -> None:
        await (runner.refresh_usage(force=True) if force else runner.refresh_usage())

    async def halt(self, conn: Any) -> None:
        await conn.interrupt()

    # ---------- 中途插话：连上了就直接交给它；刚开工还没连上就等几秒 ----------
    def steer(self, runner, s, turn, job):
        client = runner.live.get(s.num)
        return self._inject(runner, s, client, job) if client else self._inject_when_ready(runner, s, turn, job)

    async def _inject_when_ready(self, runner: "Runner", s: "Session", turn: "Turn", job: "Job") -> None:
        for _ in range(180):
            if runner.turns.get(s.num) is not turn or not turn.accepting:
                break
            if client := runner.live.get(s.num):
                return await self._inject(runner, s, client, job)
            await asyncio.sleep(0.5)
        runner.requeue(s.num, job)   # 没赶上这一轮：排到下一轮

    async def _inject(self, runner: "Runner", s: "Session", client: ClaudeSDKClient, job: "Job") -> None:
        try:
            await client.query(self.prompt(job))
            job.finished = True
        except Exception:  # noqa: BLE001 - 插不进去就排到下一轮
            log.warning("插话给 #%s 失败，排到下一轮", s.num, exc_info=True)
            runner.requeue(s.num, job)

    @staticmethod
    async def prompt(job: "Job"):
        content: list[dict] = []
        for p in job.images:
            mt = mimetypes.guess_type(p.name)[0] or "image/png"
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                         "data": base64.b64encode(p.read_bytes()).decode()}})
        if job.images:
            content.append({"type": "text", "text": "本轮参考图片的本机路径（按显示顺序，供图片工具引用）：\n" +
                            "\n".join(f"{i}. {p.resolve()}" for i, p in enumerate(job.images, 1))})
        content.append({"type": "text", "text": job.text or "（见图）"})
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    @staticmethod
    async def rewind_files(s: "Session", user_uuid: str) -> str:
        """把文件退回到你说某句话之前的样子（Claude 自己的改动记录，只有开了记录之后的轮次有）。返回空 = 成功，否则是原因。"""
        options = ClaudeAgentOptions(cwd=s.cwd, add_dirs=s.add_dirs, resume=s.claude_id, setting_sources=[],
                                     permission_mode="bypassPermissions", enable_file_checkpointing=True,
                                     extra_args={"replay-user-messages": None}, stderr=lambda line: log.debug("claude: %s", line))
        client = ClaudeSDKClient(options=options)
        try:
            await asyncio.wait_for(client.connect(), 90)
            await asyncio.wait_for(client.rewind_files(user_uuid), 90)
            return ""
        except Exception as e:  # noqa: BLE001 - 退不回就告诉你原因，对话照样回到那里
            log.warning("#%s 退回文件失败：%s", s.num, e)
            return "文件没退回（没有改动记录）"
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 钩子 ----------
    @staticmethod
    def guard(runner: "Runner", s: "Session", turn: "Turn"):
        """Bash 跑之前：危险的交给 policy 问人；同意就明确放行，不同意就拒并告诉它别绕过去。"""
        async def hook(data, tool_use_id, context):
            cmd = str((data.get("tool_input") or {}).get("command") or "")
            allow = await policy.approve(runner, s, turn, policy.EXECUTE, cmd)
            if allow is None:
                return {}
            why = turn.reviews[-1]["why"] if turn.reviews and turn.reviews[-1]["cmd"] == cmd[:300] else ""
            out = {"hookEventName": "PreToolUse"}
            if allow:
                out.update(permissionDecision="allow", permissionDecisionReason="审核通过，可以执行")
            else:
                out.update(permissionDecision="deny", permissionDecisionReason=(
                    f"这一步没有通过审核{('（' + why + '）') if why else ''}。不要执行它，也不要换个写法绕过去；"
                    "能不做就用别的办法完成，必须做就在回复里说明为什么要这样做，等用户明确同意。"))
            return {"hookSpecificOutput": out}
        return hook

    def stop_check(self, runner: "Runner", s: "Session", turn: "Turn"):
        """这一轮要结束时再看一眼（unfinished）：还差什么就拦下，让它先补上再结束（只拦一次）。"""
        async def hook(data, tool_use_id, context):
            why = self.unfinished(runner, s, turn, bool(data.get("stop_hook_active")))
            return {"decision": "block", "reason": why} if why else {}
        return hook

    @staticmethod
    def deny(names: set[str], reason: str = ""):
        """不让这个对话用某些工具（总管不亲自干活）。"""
        async def hook(data, tool_use_id, context):
            name = str(data.get("tool_name") or "")
            if name not in names:
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason or f"这个工具（{name}）在这里不能用。"}}
        return hook

    @staticmethod
    def fence(s: "Session"):
        """受限对话的护栏：只放行 USER_TOOLS；带路径的工具，路径必须在它的文件夹里（跟着符号链接算真实位置）。"""
        root = Path(s.root).expanduser().resolve()

        def inside(raw: str) -> bool:
            p = Path(str(raw)).expanduser()
            p = (p if p.is_absolute() else Path(s.cwd) / p).resolve()
            return p == root or p.is_relative_to(root)

        async def hook(data, tool_use_id, context):
            name = str(data.get("tool_name") or "")
            args = data.get("tool_input") or {}
            why = ""
            if name not in USER_TOOLS:
                why = f"这个工具（{name}）在这里不能用。"
            else:
                paths = [args[k] for k in PATH_KEYS if args.get(k)]
                pat = str(args.get("pattern") or "") if name == "Glob" else ""
                if pat.startswith(("/", "~")):
                    paths.append(pat.split("*")[0] or "/")
                if any(not inside(p) for p in paths):
                    why = f"只能在 {display(root)} 里面读写，这个路径在外面。"
            if not why:
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": why + "换成文件夹里面的做法，做不到就在回复里说明。"}}
        return hook

    @staticmethod
    def fence_options(s: "Session") -> dict:
        """受限对话额外的设置：命令在沙箱里跑（只能写自己的文件夹、连不上本机端口），家目录里别的东西读写都拒绝。"""
        root = Path(s.root).expanduser().resolve()
        home = Path.home().resolve()
        deny: list[str] = []
        outside = [Path("/Volumes"), Path("/Users/Shared")]
        if root.is_relative_to(home):   # 从文件夹往上到家目录，每一层的兄弟都挡掉
            cur = root
            while cur != home:
                try:
                    outside += [p for p in cur.parent.iterdir() if p.name != cur.name]
                except OSError:
                    pass
                cur = cur.parent
        else:
            outside.append(home)
        for p in outside:
            for t in ("Read", "Edit"):
                deny += [f"{t}(/{p})", f"{t}(/{p}/**)"]
        return {
            "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False},
            "settings": json.dumps({"permissions": {"deny": deny}}),
        }

    # ---------- 远窗给它的工具（进程内 MCP） ----------
    @staticmethod
    def tools(runner: "Runner", turn: "Turn", session: "Session | None" = None):
        @tool("send_images", "把截图或图片交给用户看。paths 是本机图片的绝对路径（png/jpg），最多 6 张，按想让用户看的顺序排。",
              {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
               "required": ["paths"]})
        async def send_images(args):
            ok, bad = [], []
            for p in args.get("paths", []):
                path = Path(p).expanduser()
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and len(turn.images) + len(ok) < 6:
                    ok.append(str(path))
                else:
                    bad.append(p)
            turn.images.extend(ok)
            msg = f"已登记 {len(ok)} 张图片，本轮结束后会显示在对话中；这不是用户已收到的回执。"
            if bad:
                msg += f" 没收下：{', '.join(bad)}（不存在、不是图片或超过 6 张）"
            return {"content": [{"type": "text", "text": msg}]}

        @tool("send_files", "把文件原样交给用户（PDF、Word、表格、压缩包等任何格式，用户能直接下载）。paths 是本机文件的绝对路径，最多 10 个，每个不超过 300MB。"
              "用户要的是文件本身时用这个，不要只发截图。",
              {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
               "required": ["paths"]})
        async def send_files(args):
            ok, bad = [], []
            for p in args.get("paths", []):
                path = Path(p).expanduser()
                if path.is_file() and path.stat().st_size <= 300 * 1024 * 1024 and len(turn.files) + len(ok) < 10:
                    ok.append(str(path))
                else:
                    bad.append(p)
            turn.files.extend(ok)
            msg = f"已登记 {len(ok)} 个文件，本轮结束后会显示下载入口；这不是用户已收到的回执。"
            if bad:
                msg += f" 没收下：{', '.join(bad)}（不存在、超过 300MB 或超过 10 个）"
            return {"content": [{"type": "text", "text": msg}]}

        @tool("update_plan", "列出或更新这件事的步骤清单，用户在手机上看进度。每次传完整清单；status 只能是 pending（没开始）、doing（正在做）、done（做完）。",
              {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "object", "properties": {
                  "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "doing", "done"]}},
                  "required": ["text", "status"]}}}, "required": ["steps"]})
        async def update_plan(args):
            turn.plan = [{"text": str(x.get("text", ""))[:60], "status": x.get("status") if x.get("status") in ("pending", "doing", "done") else "pending"}
                         for x in (args.get("steps") or [])[:20]]
            left = [x["text"] for x in turn.plan if x["status"] != "done"]
            tip = f"已更新：做完 {len(turn.plan) - len(left)}/{len(turn.plan)} 步。" + (
                f"每做完一步就马上再调一次 update_plan，把它标 done、下一步标 doing（还剩：{'、'.join(left)}）。" if left else "全部做完了。")
            return {"content": [{"type": "text", "text": tip}]}

        @tool("generate_images", "委派 Codex 使用内置生图工具生成或编辑图片，等待真实文件结果并自动登记为本轮图片附件。仅用于图片任务，不能代替一般代码任务；参考图传绝对路径，输出目录应在当前工作目录或附加目录内。成功后用 Read 检查图片再继续。",
              {"type": "object", "properties": {"prompt": {"type": "string"}, "reference_images": {"type": "array", "items": {"type": "string"}}, "output_dir": {"type": "string"}}, "required": ["prompt"]})
        async def generate_images(args):
            if session is None or turn.interrupted:
                result = {"state": "cancelled", "images": [], "error": "当前任务不可委派"}
            else:
                try:
                    result = await runner._generate_images(session, turn, args)
                except (ValueError, OSError) as exc:
                    result = {"state": "failed", "images": [], "error": str(exc)[:500]}
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": result["state"] != "completed"}

        extra = role(runner, session).get("tools", []) if session else []
        return create_sdk_mcp_server("anzhuo", tools=[send_images, send_files, update_plan] + ([] if session and session.root else [generate_images]) + extra)

    # ---------- 跑一轮 ----------
    # ---------- 换后端的引擎（DeepSeek 等，同样是 Claude Code）改这几处 ----------
    def model_for(self, s: "Session") -> str | None:
        return s.model

    def env(self, runner: "Runner", s: "Session") -> dict:
        """给 Claude Code 进程的环境变量。"""
        return {"ENABLE_TOOL_SEARCH": "false",   # 交图、交文件、步骤清单这几个工具一开始就给它，不用先搜（搜到了它也常忘了用）
                # 子代理默认用便宜一档的模型（只是默认：派的时候点名模型、或 fork 照旧跟主对话）
                **({"CLAUDE_CODE_SUBAGENT_MODEL": runner.subagent_model} if runner.subagent_model else {})}

    def extra_hooks(self, runner: "Runner", s: "Session") -> list:
        return []

    async def run(self, runner: "Runner", s: "Session", job: "Job", turn: "Turn") -> None:
        await check_folders(s)
        fenced = bool(s.root)
        if fenced:   # 受限账号：文件夹、引擎都固定，就算登记里被改过也按这里来
            s.cwd, s.add_dirs = str(Path(s.root).expanduser()), []
            Path(s.cwd).mkdir(parents=True, exist_ok=True)
        hooks = [*self.extra_hooks(runner, s), HookMatcher(matcher="Bash", hooks=[self.guard(runner, s, turn)], timeout=CONFIRM_WAIT + 60)]
        rl = role(runner, s)
        if fenced:
            hooks.insert(0, HookMatcher(matcher=None, hooks=[self.fence(s)], timeout=60))
        if rl.get("disallowed"):   # 光靠 disallowed_tools 挡不住（bypassPermissions 下照样执行），用钩子拒掉
            hooks.insert(0, HookMatcher(matcher=None, hooks=[self.deny(set(rl["disallowed"]), rl.get("deny_reason", ""))], timeout=60))
        options = ClaudeAgentOptions(
            cwd=s.cwd,
            add_dirs=s.add_dirs,
            model=self.model_for(s),
            effort=s.effort or runner.default_effort or None,
            resume=s.claude_id,
            resume_session_at=(s.rewind_at or None) if s.claude_id else None,   # 你点过“回到这里”：从那条接着，后面的不带上
            fork_session=bool(s.forking and s.claude_id),   # 分叉：从原对话那条接着，但另起一个对话编号，原对话不受影响
            enable_file_checkpointing=True,   # 记下每轮改了哪些文件，“回到这里”时能把文件一起退回
            setting_sources=[] if fenced else ["user", "project", "local"],
            permission_mode="bypassPermissions",
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": build_user_instructions(display(Path(s.root).expanduser()), runner.language, runner.extra_instructions)
                           if fenced else build_instructions(runner.language, runner.extra_instructions) + rl.get("append", "")},
            disallowed_tools=list(rl.get("disallowed", [])),
            tools=rl.get("builtin"),   # 只给这几个内置工具（总管：读文件、找文件）；None = 照常全给
            mcp_servers={"anzhuo": self.tools(runner, turn, s)},
            env=self.env(runner, s),
            stderr=lambda line: log.debug("claude: %s", line),
            max_buffer_size=64 * 1024 * 1024,  # 默认 1MB：Claude 看一张大截图就超，整轮报错（10-25 #6 实际遇到）
            thinking={"type": "adaptive", "display": "summarized"},
            include_partial_messages=True,  # 回复边写边出（网页实时看）  # 要思考摘要（默认不给字），网页“工作过程”里看它怎么想的
            # 删东西、推代码、改数据库这类先问你（见 policy.py）；等你点的时间算在钩子里，超时就当不同意
            hooks={"PreToolUse": hooks, **({"Stop": [HookMatcher(matcher=None, hooks=[self.stop_check(runner, s, turn)], timeout=60)]}
                                           if rl.get("on_stop") else {})},
            **(self.fence_options(s) if fenced else {}),
        )
        last_text = ""
        async with ClaudeSDKClient(options=options) as client:
            runner.live[s.num] = client
            try:
                last_text, results = await self._read(client, s, job, turn)
                try:
                    usage = await client.get_context_usage()
                    s.context_tokens = int(usage.get("totalTokens") or 0) or None
                    turn.context_pct = s.context_pct = self.context_pct(usage)
                except Exception:  # noqa: BLE001 - 拿不到上下文用量不影响报告
                    log.debug("取上下文用量失败", exc_info=True)
            finally:
                runner.live.pop(s.num, None)
        turn.final_text = "\n\n---\n\n".join(dict.fromkeys(results)) if len(results) > 1 else last_text
        turn.effort = s.effort or runner.default_effort

    def context_pct(self, usage: dict) -> float:
        """上下文用了百分之几：照 Claude Code 报的算；窗口另有大小的引擎（context_window）按 token 数自己折。"""
        if self.context_window and (used := int(usage.get("totalTokens") or 0)):
            return min(100.0, 100.0 * used / self.context_window)
        return float(usage["percentage"])

    async def _read(self, client: ClaudeSDKClient, s: "Session", job: "Job", turn: "Turn") -> tuple[str, list[str]]:
        """把你的话送进去，逐条读事件翻成步骤、说明、回复，直到这一轮真正结束。返回 (最后一段话, 各次结果)。"""
        await client.query(self.prompt(job))
        last_text = ""
        results: list[str] = []
        seen_injected = 0
        after_result = False
        wait_more = 4
        stream = client.receive_messages().__aiter__()
        while True:
            try:
                # 插过话又出了结果：再等几秒，插的话若没赶上这一轮，它会接着开一轮，要接着读完
                msg = await (asyncio.wait_for(stream.__anext__(), wait_more) if after_result else stream.__anext__())
            except (StopAsyncIteration, asyncio.TimeoutError):
                break
            after_result = False
            turn.last_at = time.time()
            if isinstance(msg, (TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage, TaskUpdatedMessage)):
                track_agent(turn, msg)
            elif isinstance(msg, SystemMessage):
                if "compact" in msg.subtype:
                    turn.compacted = True
                elif msg.subtype == "init" and not turn.model:
                    turn.model = str(msg.data.get("model") or "")
            elif isinstance(msg, StreamEvent):
                # 边写边看：只接主对话的正文增量（思考多是英文，等写完译好再出，免得读着换字）
                ev = msg.event or {}
                if msg.parent_tool_use_id is None:
                    if ev.get("type") == "content_block_start":
                        turn.partial = ""
                    elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                        turn.partial = (turn.partial + ev["delta"].get("text", ""))[-6000:]
            elif isinstance(msg, AssistantMessage):
                turn.partial = ""
                if msg.model:
                    turn.model = msg.model
                for b in msg.content:
                    if isinstance(b, ThinkingBlock) and b.thinking.strip():
                        turn.timeline.append({"t": time.time(), "kind": "think", "text": b.thinking[:4000]})
                texts = [b.text for b in msg.content if isinstance(b, TextBlock) and b.text.strip()]
                if texts:
                    last_text = "\n".join(texts)
                    turn.timeline.append({"t": time.time(), "kind": "say", "text": last_text[:6000]})
                sub = sub_name(turn, msg.parent_tool_use_id)
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        if label := step_label(b.name, b.input or {}):
                            turn.steps.append(label)
                            e = {"t": time.time(), "kind": "do", "text": label, "detail": _tool_detail(b.name, b.input or {})}
                            if sub:
                                e["sub"] = sub   # 助手做的步骤：过程里缩进、标上是哪个助手
                            turn.timeline.append(e)
                        if b.name in EDIT_TOOLS:
                            fp = b.input.get("file_path") or b.input.get("notebook_path")
                            if fp:
                                turn.changed_files.append(fp)
                        elif b.name == "Bash":
                            turn.commands.append(str(b.input.get("command", ""))[:200])
            elif isinstance(msg, ResultMessage):
                turn.accepting = False
                # 这一轮的消耗：CLI 报的是这个进程累计的（一轮一个进程），取最后一次
                if msg.model_usage or msg.total_cost_usd:
                    turn.usage = {"cost": float(msg.total_cost_usd or 0), "models": dict(msg.model_usage or {})}
                if msg.session_id:
                    s.claude_id = msg.session_id
                    s.rewind_at = s.rewind_note = ""   # 已经从那条接着说了，回到这里落定
                    s.forking = False   # 分叉出来的已经有了自己的对话编号
                if not msg.is_error and NOISE.match(msg.result or ""):
                    # 上一轮留下的后台任务通知先被答了一句“No response requested.”，你这句还排在后面：接着读，别当成回复
                    after_result, wait_more = True, 60
                    continue
                if running_agents(turn) and not turn.interrupted and not msg.is_error:
                    # 主对话先说完了，但还有助手在后台跑：这一轮一结束它们就被关掉，所以接着等，助手做完它会接着汇总
                    after_result, wait_more = True, AGENT_WAIT
                    continue
                wait_more = 4
                if msg.is_error and not turn.interrupted:
                    turn.is_error = True
                    turn.error = (msg.result or "; ".join(msg.errors or []) or msg.subtype or "未知错误")[:300]
                elif msg.result:
                    last_text = msg.result
                    results.append(msg.result)
                if turn.injected > seen_injected and not turn.interrupted:
                    seen_injected = turn.injected
                    after_result = True
                    continue
                break
        return last_text, results


def track_agent(turn, msg) -> None:
    """记下派出去的助手：开始、做到哪一步、结束。网页“在做”卡片和过程里按这个显示。"""
    a = turn.agents.get(msg.task_id)
    if isinstance(msg, TaskStartedMessage):
        kind = "命令" if "bash" in (msg.task_type or "") else "助手"
        turn.agents[msg.task_id] = {"name": _short_desc(msg.description), "kind": kind, "t0": time.time(), "t1": 0,
                                    "status": "running", "last": "", "tool": msg.tool_use_id}
        return
    if a is None:
        return
    if isinstance(msg, TaskProgressMessage):
        if msg.last_tool_name:
            a["last"] = (step_label(msg.last_tool_name, {}) or "").rstrip("：")
        return
    status = msg.status if isinstance(msg, TaskNotificationMessage) else (msg.status or (msg.patch or {}).get("status"))
    if status in DONE and a["status"] == "running":
        a["status"], a["t1"] = ("done" if status == "completed" else "failed" if status in ("failed", "error") else "stopped"), time.time()


def running_agents(turn) -> list[dict]:
    # 只等子代理；后台命令（开着的预览服务、Monitor 之类）可能永远不结束，等它会把对话卡住、你说的话一直排队
    return [a for a in turn.agents.values() if a["status"] == "running" and a.get("kind") == "助手"]


def sub_name(turn, parent_id) -> str:
    if not parent_id:
        return ""
    return next((a["name"] for a in turn.agents.values() if a.get("tool") == parent_id), "助手")


def _short_desc(t: str) -> str:
    t = " ".join(str(t or "").split())
    return t if len(t) <= 16 else t[:15] + "…"


def _tool_detail(name: str, inp: dict) -> str:
    """过程页里一步的细节：命令原文、文件路径、搜索词等。"""
    if name == "Bash":
        return str(inp.get("command", ""))[:3000]
    for key in ("file_path", "notebook_path", "pattern", "query", "url", "description", "skill"):
        if inp.get(key):
            return str(inp[key])[:500]
    return json.dumps(inp, ensure_ascii=False)[:500]
