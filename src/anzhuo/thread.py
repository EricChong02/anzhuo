"""把一个对话的 Claude 记录（含桌面端开的）整理成网页对话框里的一来一回：你说的话、它每轮最后的回复。
中间的工具调用不逐条列，只记步数；完整过程另有过程页。"""
from __future__ import annotations

import time
import math
from pathlib import Path

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


def full_messages(session_id: str, cwd: str | None, times: dict | None = None):
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
    if times is not None:   # 每条的时间（uuid → 秒），网页按时间把每轮记录对到回复上
        from datetime import datetime
        for e in chain:
            try:
                times[e["uuid"]] = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, AttributeError):
                pass
    out = []
    for e in chain:
        att = e.get("attachment") if e.get("type") == "attachment" else None
        if isinstance(att, dict) and att.get("type") == "queued_command" and att.get("prompt"):
            # 干活中途插的话：记录里存成“附件”，不是普通的一条，照样当你说的话显示
            prompt = att["prompt"]
            out.append(sdk.SessionMessage(type="user", uuid=e.get("uuid", ""), session_id=e.get("sessionId", ""),
                                          message={"role": "user", "content": prompt}, parent_tool_use_id=None))
        elif sdk._is_visible_message(e):
            out.append(sdk._to_session_message(e))
    return out


def user_image(session_id: str, cwd: str | None, ref: str) -> tuple[bytes, str] | None:
    """你发过的某张图（ref = 消息编号/第几张）：从对话记录里取出来给网页显示。"""
    import base64
    uuid, _, k = ref.partition("/")
    for m in full_messages(session_id, cwd):
        if getattr(m, "uuid", "") == uuid and isinstance(m.message, dict) and isinstance(m.message.get("content"), list):
            imgs = [b for b in m.message["content"] if isinstance(b, dict) and b.get("type") == "image"]
            if k.isdigit() and int(k) < len(imgs):
                src = imgs[int(k)].get("source") or {}
                if src.get("type") == "base64":
                    return base64.b64decode(src.get("data", "")), src.get("media_type") or "image/png"
    return None


def _detail(name: str, inp: dict) -> str:
    if name == "Bash":
        return str(inp.get("command", ""))[:400]
    for key in ("file_path", "notebook_path", "pattern", "query", "url", "description", "skill"):
        if inp.get(key):
            return str(inp[key])[:300]
    return ""


def history(messages, limit: int = 300, times: dict | None = None) -> list[dict]:
    items: list[dict] = []
    reply, steps, process = "", 0, []
    ts = 0.0  # 回复那段话的时间
    end = ""  # 这一轮最后一条记录的编号：“回到这里”从它接着

    def flush():
        nonlocal reply, steps, process
        if reply and reply.strip() not in _SKIP_REPLY:
            # 最后那段话是回复本身，不算进过程
            if process and process[-1]["k"] == "say" and process[-1]["text"] == reply[:1500]:
                process.pop()
            items.append({"who": "claude", "text": reply, "steps": steps, "process": process[-80:], **({"end": end} if end else {})})
            if times is not None:
                items[-1]["ts"] = ts
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
            n_img = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image") if isinstance(content, list) else 0
            if n_img and getattr(m, "uuid", ""):
                # 带图的话：网页上直接显示图（/_x/api/uimg/…），文字里去掉“[图片]”占位
                body = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
                items.append({"who": "you", "text": body, "imgs": [f"{m.uuid}/{k}" for k in range(n_img)]})
            else:
                items.append({"who": "you", "text": text.strip()})
            if getattr(m, "uuid", ""):
                items[-1]["uuid"] = m.uuid
        elif m.type == "assistant" and isinstance(content, list):
            end = getattr(m, "uuid", "") or end
            for b in content:
                kind = b.get("type") if isinstance(b, dict) else None
                if kind == "thinking" and (b.get("thinking") or "").strip():
                    process.append({"k": "think", "text": b["thinking"][:1500]})
                elif kind == "text" and b.get("text", "").strip():
                    reply = b["text"]
                    ts = (times or {}).get(getattr(m, "uuid", ""), 0.0)
                    process.append({"k": "say", "text": b["text"][:1500]})
                elif kind == "tool_use":
                    steps += 1
                    label = step_label(b.get("name", ""), b.get("input") or {})
                    if label:
                        process.append({"k": "do", "text": label, "detail": _detail(b.get("name", ""), b.get("input") or {})})
    flush()
    # 压缩点之前的回复不能“回到这里”（要把压缩前的整段重新带上，上下文会撑爆）
    last_note = max((i for i, it in enumerate(items) if it["who"] == "note"), default=-1)
    for it in items[:last_note + 1]:
        it.pop("end", None)
    out = items[-limit:]
    replies = [it for it in out if it["who"] == "claude"]
    for it in replies[:-PROCESS_KEEP]:
        it.pop("process", None)
    return out


def attach_records(items: list[dict], records: list[dict]) -> list[dict]:
    """把远窗记下的每轮记录（过程页编号、文件、图片）按先后对到最近几轮回复上。"""
    replies = [it for it in items if it["who"] == "claude"]
    if replies and all(it.get("ts") for it in replies):
        # 按时间对：回复写在哪一轮的起止时间里就是哪一轮的（正在做的那轮还没记录，按顺序对会整体错一位）
        pairs, free = [], list(reversed(replies))
        for rec in reversed(records):
            lo, hi = rec.get("started") or 0, (rec.get("ended") or time.time()) + 10
            hit = next((it for it in free if it.get("end") == rec["id"]), None)
            if hit is None:
                hit = next((it for it in free if lo - 2 <= it["ts"] <= hi), None)
            if hit:
                free.remove(hit)
                pairs.append((hit, rec))
    else:
        pairs = list(zip(reversed(replies), reversed(records)))
    for it, rec in pairs:
        it["model"], it["effort"] = rec.get("model", ""), rec.get("effort", "")
        started, ended = rec.get("started"), rec.get("ended")
        if all(isinstance(t, (int, float)) and math.isfinite(t) for t in (started, ended)) and ended >= started:
            it["elapsed"] = ended - started
        it["tid"] = rec["id"]
        it["title"] = rec.get("title", "")
        it["images"] = rec.get("images", [])
        it["files"] = rec.get("files", [])
        it["nfiles"], it["ncmds"] = rec.get("nfiles", 0), rec.get("ncmds", 0)
        if rec.get("final_text") and rec.get("translated"):
            it["text"] = rec["final_text"]  # 钉钉里发的是译好的中文，网页也用它
    return items


_PATHS: dict[str, Path] = {}


def transcript_path(session_id: str) -> Path | None:
    """Claude 对话记录文件在哪（搜索时按它的修改时间判断要不要重读）。"""
    p = _PATHS.get(session_id)
    if p and p.exists():
        return p
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    if hits:
        _PATHS[session_id] = hits[0]
        return hits[0]
    return None
