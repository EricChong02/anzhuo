"""把一个对话的 Claude 记录（含桌面端开的）整理成网页对话框里的一来一回：你说的话、它每轮最后的回复。
中间的工具调用不逐条列，只记步数；完整过程另有过程页。"""
from __future__ import annotations

_SKIP_PREFIX = ("<command-", "<local-command", "<system-reminder", "Caveat:", "<bash-", "<task-notification",
                "[SYSTEM NOTIFICATION")
_SKIP_REPLY = {"No response requested.", "No response requested"}  # 系统通知那一轮的自动回复，不是给你的


def _user_text(content) -> str | None:
    """用户这一条要显示的文字；只有工具结果（不是你说的话）时返回 None。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
    if not texts and not images:
        return None
    head = [f"[{images} 张图片]" if images > 1 else "[图片]"] if images else []
    return "\n".join(p for p in head + texts if p)


def history(messages, limit: int = 60) -> list[dict]:
    items: list[dict] = []
    reply, steps = "", 0

    def flush():
        nonlocal reply, steps
        if reply and reply.strip() not in _SKIP_REPLY:
            items.append({"who": "claude", "text": reply, "steps": steps})
        reply, steps = "", 0

    for m in messages:
        msg = m.message if isinstance(m.message, dict) else {}
        content = msg.get("content")
        if m.type == "user":
            text = _user_text(content)
            if text is None or not text.strip() or text.lstrip().startswith(_SKIP_PREFIX):
                continue
            flush()
            if text.startswith("This session is being continued"):
                items.append({"who": "note", "text": "（更早的对话内容已压缩）"})
                continue
            items.append({"who": "you", "text": text.strip()})
        elif m.type == "assistant" and isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                     and b.get("text", "").strip()]
            if texts:
                reply = "\n".join(texts)
            steps += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
    flush()
    return items[-limit:]


def attach_records(items: list[dict], records: list[dict]) -> list[dict]:
    """把小E 记下的每轮记录（过程页编号、试课链接）按先后对到最近几轮回复上。"""
    replies = [it for it in items if it["who"] == "claude"]
    for it, rec in zip(reversed(replies), reversed(records)):
        it["tid"] = rec["id"]
        it["previews"] = rec.get("previews", [])
        it["title"] = rec.get("title", "")
        if rec.get("final_text") and rec.get("translated"):
            it["text"] = rec["final_text"]  # 钉钉里发的是译好的中文，网页也用它
    return items
