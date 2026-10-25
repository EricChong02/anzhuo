"""把一个对话的 Claude 记录（含桌面端开的）整理成网页对话框里的一来一回：你说的话、它每轮最后的回复。
中间的工具调用不逐条列，只记步数；完整过程另有过程页。"""
from __future__ import annotations

from .cards import step_label

PROCESS_KEEP = 15  # 最近几轮带“工作过程”（再早的看过程页），免得一个长对话整页太重

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


def full_messages(session_id: str, cwd: str | None):
    """读整段对话记录，压缩过的也接上压缩前的部分。
    SDK 的 get_session_messages 只从最近一次压缩往后读（压缩点之前的被摘要顶替），网页上就看不到早先的对话了；
    这里顺着压缩点的 logicalParentUuid 把前面几段接回去，压缩摘要本身显示成一条“已压缩”提示。"""
    from claude_agent_sdk._internal import sessions as sdk

    content = sdk._read_session_file(session_id, cwd)
    if not content:
        return []
    entries = sdk._parse_transcript_entries(content)
    by_uuid = {e["uuid"]: e for e in entries}
    chain = sdk._build_conversation_chain(entries)
    seen = {e["uuid"] for e in chain}
    while chain:
        first = chain[0]
        boundary = first if first.get("logicalParentUuid") else by_uuid.get(first.get("parentUuid") or "")
        prev = by_uuid.get((boundary or {}).get("logicalParentUuid") or "")
        if not prev or prev["uuid"] in seen:
            break
        part, cur = [], prev
        while cur is not None and cur["uuid"] not in seen:
            seen.add(cur["uuid"])
            part.append(cur)
            cur = by_uuid.get(cur.get("parentUuid") or "")
        chain = part[::-1] + chain
    return [sdk._to_session_message(e) for e in chain if sdk._is_visible_message(e)]


def _detail(name: str, inp: dict) -> str:
    if name == "Bash":
        return str(inp.get("command", ""))[:400]
    for key in ("file_path", "notebook_path", "pattern", "query", "url", "description", "skill"):
        if inp.get(key):
            return str(inp[key])[:300]
    return ""


def history(messages, limit: int = 300) -> list[dict]:
    items: list[dict] = []
    reply, steps, process = "", 0, []

    def flush():
        nonlocal reply, steps, process
        if reply and reply.strip() not in _SKIP_REPLY:
            # 最后那段话是回复本身，不算进过程
            if process and process[-1]["k"] == "say" and process[-1]["text"] == reply[:1500]:
                process.pop()
            items.append({"who": "claude", "text": reply, "steps": steps, "process": process[-80:]})
        reply, steps, process = "", 0, []

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
            for b in content:
                kind = b.get("type") if isinstance(b, dict) else None
                if kind == "thinking" and (b.get("thinking") or "").strip():
                    process.append({"k": "think", "text": b["thinking"][:1500]})
                elif kind == "text" and b.get("text", "").strip():
                    reply = b["text"]
                    process.append({"k": "say", "text": b["text"][:1500]})
                elif kind == "tool_use":
                    steps += 1
                    label = step_label(b.get("name", ""), b.get("input") or {})
                    if label:
                        process.append({"k": "do", "text": label, "detail": _detail(b.get("name", ""), b.get("input") or {})})
    flush()
    out = items[-limit:]
    replies = [it for it in out if it["who"] == "claude"]
    for it in replies[:-PROCESS_KEEP]:
        it.pop("process", None)
    return out


def attach_records(items: list[dict], records: list[dict]) -> list[dict]:
    """把小E 记下的每轮记录（过程页编号、试课链接）按先后对到最近几轮回复上。"""
    replies = [it for it in items if it["who"] == "claude"]
    for it, rec in zip(reversed(replies), reversed(records)):
        it["tid"] = rec["id"]
        it["previews"] = rec.get("previews", [])
        it["title"] = rec.get("title", "")
        it["images"] = rec.get("images", [])
        it["files"] = rec.get("files", [])
        if rec.get("final_text") and rec.get("translated"):
            it["text"] = rec["final_text"]  # 钉钉里发的是译好的中文，网页也用它
    return items
