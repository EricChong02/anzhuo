"""接力：长对话写交接说明、换到新对话接着做。这里放交接提示词和“该不该提示接力”的判断，app 和 web 共用。"""
from __future__ import annotations

import time

from . import engines
from .config import Config

# 接力：让长对话写一份交接说明，换到新对话接着做（新对话不用背着几十万 token 的旧记录）
RELAY = """这个对话太长了，要换到一个新对话接着做。请写一份交接说明，新对话只靠它接手，所以要具体：
1. 目标：用户要做成什么、明确提过的要求和偏好；
2. 已定下的决定和理由（含否掉的做法，免得重走）；
3. 已经做完的：改了哪些文件、提交 / 推送 / 上线情况；
4. 正在做的和接下来几步；
5. 关键路径、命令、账号位置、踩过的坑；
6. 还等用户决定的问题。
只回复交接说明本身，用 Markdown，2000 字以内；不要调用任何工具，不要再做别的事。"""

CACHE_TTL = 3300       # Claude 的提示缓存约 1 小时过期，留 5 分钟余量


def relay_hint(s, busy: bool, relaying: bool, now: float | None = None, cfg=None) -> dict | None:
    """该不该提示“接力到新对话”。返回 {kind: long/cold, tokens, hours, key}；key 用来记你点过“不用”。
    门槛见配置 relay_long（上下文太长）、relay_cold（缓存过期又不短）。"""
    if relaying:
        return {"kind": "running"}
    long_at, cold_at = getattr(cfg, "relay_long", Config.relay_long), getattr(cfg, "relay_cold", Config.relay_cold)
    tk = s.context_tokens or 0
    if busy or s.relay_to or not (s.claude_id or s.codex_id) or tk < cold_at:
        return None
    idle = (now or time.time()) - s.updated
    if engines.get(s.engine).cache_hint and idle > CACHE_TTL:
        return {"kind": "cold", "tokens": tk, "hours": round(idle / 3600, 1), "key": f"cold{int(s.updated)}"}
    if tk >= long_at:
        return {"kind": "long", "tokens": tk, "key": f"long{tk // 100_000}"}
    return None


def display_items(items):
    """隐藏内部接力轮次；第一句话只展示用户原话，兼容既有记录。"""
    out, internal = [], False
    for original in items:
        it = dict(original)
        if it.get("who") == "you":
            internal = it.get("text", "").strip() == RELAY
            if internal:
                continue
            text = it.get("text", "")
            if text.startswith("（这是接力的新对话") and "用户这次说：\n\n" in text:
                it["text"] = text.split("用户这次说：\n\n", 1)[1]
        if not internal:
            out.append(it)
    return out
