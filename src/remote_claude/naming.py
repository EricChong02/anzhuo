"""给对话起短名字：头一轮做完后，按你要做的事和它的回复，用 DeepSeek 起一个短名字，失败保留原名。"""
from __future__ import annotations

import asyncio
import logging
import re

from . import deepseek_api

log = logging.getLogger(__name__)

MODEL = deepseek_api.MODEL
SYSTEM = """给一段工作对话起名字，让人在列表里一眼看出在做什么。只输出名字本身，不要引号、标点结尾或解释。
简体中文，4 到 10 个字，手机列表一行要能显示完；说清对象和在做的事，比如“大数据第二节磨课”“厦门天气”“讲座课件制作”“远窗列表改版”。
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
    key = await asyncio.to_thread(deepseek_api.key)
    if not key:
        return ""
    prompt = f"用户交代的事：\n{asked[:400]}\n\n助手的回复（节选）：\n{answer[:400]}"
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(deepseek_api.chat, SYSTEM, prompt, key,
            timeout=timeout, max_tokens=128, purpose='naming'), timeout)
    except Exception as exc:
        log.warning('自动起名失败，保留原名：%s', type(exc).__name__)
        return ""
    t = _clean(raw)
    return t if valid(t) else ""
