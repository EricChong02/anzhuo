"""给对话起短名字：头一轮做完后，按你要做的事和它的回复，用最便宜的模型起一个十几个字的名字。"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
SYSTEM = """给一段工作对话起名字，让人在列表里一眼看出在做什么。只输出名字本身，不要引号、标点结尾或解释。
简体中文，4 到 10 个字，手机列表一行要能显示完；说清对象和在做的事，比如“大数据第二节磨课”“厦门天气”“讲座课件制作”“案头列表改版”。
不用标点、引号和“·”，不要“关于”“讨论”“对话”这类空词。"""


_BAD = re.compile(r"会话|名字|标题|对话名|起名|title|here", re.I)


def _clean(raw: str) -> str:
    t = raw.strip().splitlines()[0] if raw.strip() else ""
    t = re.sub(r"^.{0,8}?(?:名字|标题|名称)\s*[:：]\s*", "", t)   # “大会话名字：问候打招呼” → “问候打招呼”
    t = re.sub(r"^[#*\s“\"'《]*", "", t).strip(" “”\"'。.，,》")
    return re.sub(r"[·•。，,、]", "", t)[:12]


def valid(name: str) -> bool:
    """像个名字：4–10 个字、以中文为主，不是“Here's a title”“会话名字”这类没起好的。"""
    t = (name or "").strip()
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    return 4 <= len(t) <= 10 and cjk * 2 >= len(t) and not _BAD.search(t)


async def short_title(asked: str, answer: str, timeout: float = 60) -> str:
    prompt = f"用户交代的事：\n{asked[:1500]}\n\n助手的回复（节选）：\n{answer[:1500]}"
    options = ClaudeAgentOptions(model=MODEL, effort="low", system_prompt=SYSTEM, tools=[], max_turns=1,
                                 setting_sources=[], cwd=str(Path.home()),
                                 extra_args={"no-session-persistence": None})
    out: list[str] = []

    async def run():
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                out.extend(b.text for b in msg.content if isinstance(b, TextBlock))

    try:
        await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001 - 起不了名就留原来的
        log.warning("自动起名失败", exc_info=True)
        return ""
    t = _clean("".join(out))
    return t if valid(t) else ""
