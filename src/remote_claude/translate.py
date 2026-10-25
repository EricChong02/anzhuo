"""Claude 回了大段英文时，发钉钉前用 Haiku 译成中文；专有名词、代码、命令、路径保持原样。"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from .router import ROUTER_EFFORT, ROUTER_MODEL

log = logging.getLogger(__name__)

# 连着 8 个以上英文单词才算“一段英文”；零散的专有名词、命令不算
_ENGLISH_RUN = re.compile(r"[A-Za-z][A-Za-z'’\-]*(?:[ ,;:]+[A-Za-z][A-Za-z'’\-]*){7,}")
_NOT_PROSE = re.compile(r"```.*?```|`[^`\n]*`|https?://\S+|(?:~|\.{0,2})/[\w./\-]+", re.S)

SYSTEM = """把用户给的文字翻译成简体中文，只输出译文，不加任何说明。
- 已经是中文的部分原样保留。
- 专有名词（产品、软件、模型、函数、文件名）、代码块、行内代码、命令、路径、网址保持英文原样。
- Markdown 结构（标题、列表、表格、加粗、分隔线）保持不变。
- 译文自然口语，写给不看代码的读者。"""


def needs_translation(text: str) -> bool:
    return bool(_ENGLISH_RUN.search(_NOT_PROSE.sub(" ", text or "")))


async def to_chinese(text: str, timeout: float = 90) -> str | None:
    """返回译文；出错或超时返回 None，由调用方照发原文。"""
    options = ClaudeAgentOptions(model=ROUTER_MODEL, effort=ROUTER_EFFORT, system_prompt=SYSTEM,
                                 tools=[], max_turns=1, setting_sources=[], cwd=str(Path.home()))
    out: list[str] = []

    async def run():
        async for msg in query(prompt=text, options=options):
            if isinstance(msg, AssistantMessage):
                out.extend(b.text for b in msg.content if isinstance(b, TextBlock))

    try:
        await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001 - 译不了就发原文
        log.warning("翻译失败，照发原文", exc_info=True)
        return None
    zh = "".join(out).strip()
    return zh or None
