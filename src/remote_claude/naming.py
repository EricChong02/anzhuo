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
简体中文，6 到 16 个字；具体到对象（哪门课、哪个功能、哪份文件），可以用“ · ”隔开两段，比如“大数据第二节 · 第 3 屏错位”。"""


def _clean(raw: str) -> str:
    t = raw.strip().splitlines()[0] if raw.strip() else ""
    t = re.sub(r"^[#*\s“\"'《]*(名字|标题)?[:：]?\s*", "", t).strip(" “”\"'。.，,")
    return t[:24]


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
    return t if len(t) >= 4 else ""
