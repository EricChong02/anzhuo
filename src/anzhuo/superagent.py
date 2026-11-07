"""总管（super_agent，默认关闭）：你只跟一个对话说话，它替你开新对话派活、转达后续的话、汇总回报。

思路来自 DeepSeek 的 super-agent 原型：总管只管路由，不亲自干活；派出去的对话各干各的；
它们做完总管派的活先回报给总管，总管再用一两句话告诉你。总管不等、不轮询，回报会自己来。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import tool

from . import folders
from .dingtalk import Incoming
from .sessions import Session

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger(__name__)

ROUTER = "router"
NAME = "总管"
VIA = "router"   # 总管派的活
REPORT = "report"   # 派出去的对话回报给总管

RULES = """
你是“总管”。用户只跟你说话，你替用户管其他对话：开新对话派活、把后续的话转给对应的对话、汇总回报。
- 你只路由，不亲自干活：不改文件、不跑命令、不替对话设计方案或拆步骤。真正的活交给对话去做。
- 派活时把用户的原话原样交出去（可以在前面加一句不超过 30 字的背景，比如“接着 #12 的结果”），不要改写成你自己的计划。
- 先用 list_sessions 看看有没有正在做同一件事的对话：用户明确指着某个对话（说了编号、说“刚才那个”且只对得上一个）就用 send_to_session 交给它；只是话题相近，算新的事，用 new_session 新开。
- 用户点名用哪个引擎（Claude / GPT）或模型就照办；没点名就不填，用默认。
- 派完就结束这一轮，用一句话告诉用户交给了谁（#编号）。不要等、不要反复查状态：对话做完、要用户定、出错时会自动给你发“回报”。
- 收到回报：做完了就用一两句话告诉用户结果和去哪看（#编号）；对话在等用户定，就把它的问题原样转给用户，一次只问一个；出错就说清楚卡在哪。同一件事已经说过的进展不要重复说。
- 用户回答了某个对话的问题，用 send_to_session 把用户的原话转过去。
- 用户问“现在都在干嘛”，用 list_sessions 汇总，只说需要用户关心的。
"""

# 总管不亲自干活：改文件、跑命令的工具不给它（读和找文件夹还可以）
DISALLOWED = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Task", "Agent"]


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)}]}


def _error(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def state(bridge: "Bridge", s: Session) -> str:
    if s.status == "error":
        return "出错"
    if bridge.runner.busy(s.num):
        return "在做" if s.num in bridge.runner.turns else "排队"
    if s.awaiting:
        return "等用户定" if s.awaiting_kind == "decide" else "做完了（顺口问要不要再做点什么）"
    return "空闲"


def brief(bridge: "Bridge", s: Session) -> dict:
    return {"num": s.num, "name": s.name, "folder": folders.display(Path(s.cwd)), "state": state(bridge, s),
            "engine": "GPT" if s.engine == "codex" else "Claude", "awaiting": s.awaiting,
            "last": s.last_summary, "from_router": bool(s.boss)}


def find(bridge: "Bridge", owner: str = "admin") -> Session | None:
    return next((s for s in bridge.registry.sessions.values() if s.role == ROUTER and s.owner == owner and not s.hidden), None)


def ensure(bridge: "Bridge") -> Session:
    """打开 super_agent 时保证有一个总管对话（置顶、固定用 Claude、在自己的文件夹里）。"""
    if s := find(bridge):
        return s
    from .app import MODELS
    num = bridge.registry.reserve_num()
    cwd = bridge.data_dir / "router"
    cwd.mkdir(parents=True, exist_ok=True)
    s = Session(num=num, name=NAME, cwd=str(cwd), model=MODELS.get(bridge.cfg.simple_model, bridge.cfg.simple_model),
                role=ROUTER, pinned=True, renamed=True, titled=True, worked=True)
    bridge.registry.add(s)
    bridge.registry.save()
    log.info("总管已开好：#%s", num)
    return s


def role(bridge: "Bridge", s: Session | None) -> dict | None:
    if not (bridge.cfg.super_agent and s and s.role == ROUTER):
        return None
    return {"tools": tools(bridge, s), "append": RULES, "disallowed": DISALLOWED}


def _incoming(text: str, via: str) -> Incoming:
    return Incoming(sender_id="local", sender_nick=NAME, conversation_type="1", kind="text", text=text, via=via)


def tools(bridge: "Bridge", router: Session) -> list:
    reg = bridge.registry

    def worker(num) -> Session | None:
        try:
            s = reg.mine(int(num), router.owner)
        except (TypeError, ValueError):
            return None
        return None if not s or s.role == ROUTER else s

    @tool("list_sessions", "列出用户的对话（不含你自己）：编号、名字、文件夹、状态、在等用户定什么、上一轮说了什么。"
          "默认只列最近的 20 个。",
          {"type": "object", "properties": {"limit": {"type": "integer"}}})
    async def list_sessions(args):
        items = sorted((x for x in reg.visible(router.owner) if x.role != ROUTER and not x.hidden), key=lambda x: -x.updated)
        limit = max(1, min(int(args.get("limit") or 20), 50))
        return _text([brief(bridge, x) for x in items[:limit]])

    @tool("new_session", "开一个新对话去做一件事，立刻开工。task 写用户的原话（可加一句背景）；"
          "folder 可写文件夹短名、完整路径或文件夹名里的几个字，不写就用临时文件夹；"
          "engine 只在用户点名时填：claude 或 gpt；model 只在用户点名时填：opus / sonnet / haiku / fable。",
          {"type": "object", "properties": {"task": {"type": "string"}, "folder": {"type": "string"},
                                            "engine": {"type": "string", "enum": ["claude", "gpt"]},
                                            "model": {"type": "string"}}, "required": ["task"]})
    async def new_session(args):
        from .app import MODELS
        task = str(args.get("task") or "").strip()
        if not task:
            return _error("task 不能为空")
        spec = str(args.get("folder") or "").strip()
        if spec:
            cands = folders.candidates(spec, bridge.cfg)
            if not cands:
                return _error(f"没找到文件夹“{spec}”。可以写短名（{'、'.join(bridge.cfg.folders) or '无'}）、完整路径，或不写用临时文件夹。")
            if len(cands) > 1:
                return _error("文件夹有几个对得上，问用户选哪个，或换成完整路径：\n" + "\n".join(str(p) for p in cands[:8]))
            cwd = cands[0]
        else:
            cwd = None
        model = "gpt" if args.get("engine") == "gpt" else MODELS.get(str(args.get("model") or "").lower())
        num = reg.reserve_num()
        if cwd is None:
            cwd = folders.scratch_dir(bridge.cfg, num)
        await bridge._start(num, cwd, [], task, _incoming(task, VIA), model=model, owner=router.owner, boss=router.num)
        s = reg.get(num)
        if not s:
            return _error("没开成")
        return _text(f"已开 #{s.num} {s.name}，在 {folders.display(Path(s.cwd))} 开工了。做完、要用户定或出错时会回报给你。")

    @tool("send_to_session", "把一句话交给某个已有对话（用户的后续要求、对它问题的回答）。它正在做就插话，没在做就开始新一轮。",
          {"type": "object", "properties": {"num": {"type": "integer"}, "text": {"type": "string"}}, "required": ["num", "text"]})
    async def send_to_session(args):
        s = worker(args.get("num"))
        text = str(args.get("text") or "").strip()
        if not s:
            return _error(f"没有 #{args.get('num')} 这个对话")
        if not text:
            return _error("text 不能为空")
        s.boss = router.num   # 之后它做完这件也回报给总管
        s.hidden = False
        await bridge._submit(s, text, _incoming(text, VIA))
        return _text(f"已交给 #{s.num} {s.name}。")

    @tool("session_status", "看某个对话的现状和它最后一次的回复（最多 3000 字）。只在用户问起、或回报里信息不够时用。",
          {"type": "object", "properties": {"num": {"type": "integer"}}, "required": ["num"]})
    async def session_status(args):
        s = worker(args.get("num"))
        if not s:
            return _error(f"没有 #{args.get('num')} 这个对话")
        info = brief(bridge, s)
        last = bridge.runner.last.get(s.num)
        info["reply"] = (last.final_text or "")[:3000] if last else ""
        return _text(info)

    @tool("stop_session", "让某个对话停下（用户要求停时才用）。",
          {"type": "object", "properties": {"num": {"type": "integer"}}, "required": ["num"]})
    async def stop_session(args):
        s = worker(args.get("num"))
        if not s:
            return _error(f"没有 #{args.get('num')} 这个对话")
        stopped = await bridge.runner.stop(s.num)
        return _text(f"#{s.num} {'已让它停下' if stopped else '本来就没在做，排队的也清掉了'}。")

    return [list_sessions, new_session, send_to_session, session_status, stop_session]


def report_text(s: Session, turn) -> str:
    """派出去的对话做完一轮：给总管的回报。"""
    if turn.is_error or turn.interrupted:
        head = "出错了" if turn.is_error else "被停下了"
        body = (turn.error or turn.final_text or "")[:300]
    elif s.awaiting and s.awaiting_kind == "decide":
        head, body = "在等用户定", s.awaiting
    else:
        head, body = "做完了", (turn.final_text or "")[:600]
    files = "；改了 " + "、".join(Path(f).name for f in list(dict.fromkeys(turn.changed_files))[:8]) if turn.changed_files else ""
    return f"【回报】#{s.num} {s.name}：{head}{files}\n\n{body}"


async def on_turn_done(bridge: "Bridge", s: Session, turn) -> bool:
    """总管派的活做完一轮：回报给总管，返回 True（这一轮不再直接通知用户，由总管来说）。"""
    if not bridge.cfg.super_agent or s.role == ROUTER or not s.boss or turn.via != VIA:
        return False
    router = bridge.registry.get(s.boss)
    if not router or router.role != ROUTER or router.hidden:
        return False
    try:
        await bridge._submit(router, report_text(s, turn), _incoming("", REPORT))
    except Exception:  # noqa: BLE001 - 回报失败就照旧直接通知用户
        log.exception("回报给总管失败 #%s", s.num)
        return False
    return True
