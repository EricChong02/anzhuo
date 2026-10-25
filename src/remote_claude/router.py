"""你直接说话时，用最便宜的模型判断：接着哪个对话，还是新开一个、在哪个文件夹。拿不准就让小E问你。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from .commands import HELP
from .folders import display
from .sessions import Session

log = logging.getLogger(__name__)

ROUTER_MODEL = "claude-haiku-5-5"
ROUTER_EFFORT = "low"  # 小E 自己的判断只用低档思考；干活的对话另按配置（默认中）

SYSTEM = """你是一个分派员。用户在手机上给他 Mac 上的几个工作对话发消息，你判断这条消息该交给谁。
只输出一个 JSON 对象，不要任何别的文字：
{"action": "reply" | "continue" | "new" | "ask", "target": 对话编号或 null, "folder": 文件夹或 null, "kind": "dev" | "simple" | null, "answer": 文字或 null, "reason": "十个字以内"}
- reply：打招呼、闲聊、谢谢、问你是谁、问怎么用这类，不需要任何对话的内容、也不用动手做事，你直接回答。answer 写给用户的回答，简体中文，口语，一到三句话；问用法时按下面的用法说明答。
- continue：消息是在接着某个已有对话说（追问、补充、修改意见、“继续”、“好的”、“选B”、对刚做完的事提要求等）。target 写那个对话的编号；多半是“当前对话”。
- new：消息是一件和已有对话都不相干的新事。folder 写最合适的文件夹：优先用给出的短名；否则写用户提到的文件夹名或其中几个字；完全不涉及文件夹（查资料、算东西、闲聊）就写 null。
- ask：确实拿不准是接着还是新开，或看不出是接哪个对话。
判断主要看话题：消息和哪个已有对话的内容相关（同一个文件夹、同一门课、同一件事），就 continue 那个对话，不一定是当前对话。
只有像是对上一条回答的直接回复（“好的”“选B”“继续”“再大一点”“为什么”这类，看不出话题），才 continue 当前对话。
打招呼、闲聊这类不是任务，不要 new；要用到某个对话内容的（“刚才为什么这么改”“上面那个再解释下”）选 continue。
只有用户要办一件和所有已有对话都不相关的事，才选 new。
如果给了“你刚才直接回答用户的”，而这条消息是在接着那段话说（追问、纠正、补充，比如“她老公不是老师”“那如果是…呢”），就选 reply，结合前文接着回答；那段话要用到文件或动手做事时才选 new。
选 new 时再给 kind：开发类工作（写代码、改程序、做功能、调试、做课件或课程页面，不限哪个项目）写 "dev"；
简单任务（查资料、整理文件、问答、写一段文字、算东西）写 "simple"。其他情况 kind 写 null。"""


@dataclass
class Route:
    action: str  # continue / new / ask
    target: int | None = None
    folder: str | None = None
    reason: str = ""
    kind: str | None = None  # 新事是开发（dev）还是简单任务（simple）
    answer: str = ""  # action 为 reply 时小E直接回的话


def _context(text: str, sessions: list[Session], current: int | None, shortnames: list[str],
             chat: list[tuple[str, str]] | None = None) -> str:
    rows = []
    for s in sorted(sessions, key=lambda x: -x.updated)[:8]:
        mins = int((time.time() - s.updated) / 60)
        cur = "（当前对话）" if s.num == current else ""
        last = f"，上次：{s.last_title}" if s.last_title else ""
        rows.append(f"- #{s.num}{cur} {s.name}｜文件夹 {display(Path(s.cwd))}｜{mins} 分钟前{last}")
    said = "".join(f"\n用户：{q[:300]}\n你：{a[:400]}" for q, a in chat or [])
    return (
        (f"你刚才直接回答用户的（从早到晚）：{said}\n\n" if said else "")
        + "已有对话：\n" + ("\n".join(rows) if rows else "（没有）")
        + f"\n\n文件夹短名：{'、'.join(shortnames)}\n\n用户这条消息：\n{text[:1500]}"
    )


def _parse(raw: str, valid: set[int]) -> Route | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    action = j.get("action")
    if action not in ("reply", "continue", "new", "ask"):
        return None
    target = j.get("target")
    target = int(target) if isinstance(target, (int, str)) and str(target).lstrip("#").isdigit() else None
    if isinstance(target, int) and target not in valid:
        target = None
    if action == "continue" and target is None:
        action = "ask"
    folder = j.get("folder")
    folder = str(folder).strip() if folder not in (None, "", "null") else None
    kind = j.get("kind") if j.get("kind") in ("dev", "simple") else None
    answer = str(j.get("answer") or "").strip()
    if action == "reply" and not answer:
        action = "continue" if target else "ask"
    return Route(action, target, folder, str(j.get("reason") or "")[:20], kind, answer[:400])


async def route(text: str, sessions: list[Session], current: int | None, shortnames: list[str],
                timeout: float = 25, chat: list[tuple[str, str]] | None = None) -> Route | None:
    """返回判断结果；出错、超时、答非所问时返回 None，由调用方按老规矩交给当前对话。"""
    options = ClaudeAgentOptions(
        model=ROUTER_MODEL, effort=ROUTER_EFFORT, system_prompt=SYSTEM + "\n\n你叫小E，是用户钉钉里的助手，背后是用户 Mac 上的 Claude Code。称呼用户时用“你”。用法说明：\n" + HELP,
        tools=[], max_turns=1,
        setting_sources=[], cwd=str(Path.home()),
    )
    out = []

    async def run():
        async for msg in query(prompt=_context(text, sessions, current, shortnames, chat), options=options):
            if isinstance(msg, AssistantMessage):
                out.extend(b.text for b in msg.content if isinstance(b, TextBlock))

    try:
        await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001 - 判断失败就退回老规矩
        log.warning("自动判断失败", exc_info=True)
        return None
    r = _parse("".join(out), {s.num for s in sessions})
    log.info("自动判断：%s", r)
    return r
