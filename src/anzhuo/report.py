"""把一轮工作的结果排成钉钉消息：标题一行，正文是 Claude 的回复原文（不改写、不删段），再附钉钉桥自己看到的实际改动。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .folders import display

MAX_LEN = 3500  # 钉钉 Markdown 单条留足余量；超出拆成几条


@dataclass
class Turn:
    started: float
    checkpoint_before: str = ""
    checkpoint_after: str = ""
    schedule_id: str = ""
    schedule_run_id: str = ""
    codex_turn_id: str = ""
    request: str = ""  # 这一轮你说的话的开头，做标题
    said: str = ""  # 这一轮你说的完整的话（存进每轮记录；额度用完接力时远窗拼交接材料用）
    ended: float = 0.0
    changed_files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)  # 每一步说成人话，给进度卡用
    timeline: list[dict] = field(default_factory=list)  # 完整过程：说了什么、做了什么（过程页用）
    links: list[tuple[str, str]] = field(default_factory=list)  # 报告末尾的链接（完整过程、扩展附的链接）
    injected: int = 0  # 做的过程中你插了几次话
    accepting: bool = True  # 还能插话（出结果后就不能了，再说的话排到下一轮）
    images: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # 交给你的文件（PDF、文档等，原样）
    final_text: str = ""
    is_error: bool = False
    error: str = ""
    interrupted: bool = False
    model: str = ""
    effort: str = ""
    context_pct: float | None = None
    compacted: bool = False
    translated: str = ""  # 原文有大段英文时，谁译成了中文（DeepSeek / Haiku）
    partial: str = ""  # 正在写的那段回复（边写边给网页看；写完进 timeline 后清空）
    reviews: list = field(default_factory=list)  # 审核员替你判断过的危险命令：{"allow", "what", "why", "cmd"}
    plan: list = field(default_factory=list)  # 这件事拆成的几步：{text, status: pending/doing/done}
    agents: dict = field(default_factory=dict)  # 派出去的助手（子代理、后台命令）：task_id → {name, t0, t1, status, last}
    confirm: dict | None = None  # 正在等你确认的危险操作：{"what": 人话, "cmd": 命令, "fut": 等你点的结果}
    last_at: float = 0.0  # 最近一次有动静（新的一步、一段话、助手进展）：网页据此提示“好一会儿没动静了”
    usage: dict = field(default_factory=dict)  # 这一轮的消耗：Claude {cost, models}；Codex {codex: 会话累计 token}
    via: str = ""  # 这一轮是从哪说的：web / dingtalk / 扩展名（钉钉里说的，结果照旧全发回钉钉）


EFFORT_CN = {"low": "低", "medium": "中", "high": "高", "xhigh": "很高", "max": "最高"}
LIMIT_CN = {"five_hour": "5 小时", "seven_day": "本周", "seven_day_fable": "本周 Fable", "seven_day_opus": "本周 Opus", "seven_day_sonnet": "本周 Sonnet"}


def brief(text: str, limit: int = 20) -> str:
    """取第一句话做短标题：到句末标点为止；还太长就截断加省略号。"""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    m = re.match(r"(.+?[。？！?!；;])", first)
    if m and len(m.group(1)) <= limit:
        return m.group(1).rstrip("。；;")
    return first[:limit] + ("…" if len(first) > limit else "")


_SECTIONS = ("要你做的", "要你定", "做了什么", "改了哪些", "自查结果", "截图")


def _plain(line: str) -> str:
    line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)  # 链接只留文字
    line = re.sub(r"[*`]+", "", line)
    return re.sub(r"^\s*(?:#+|[-•>]|\d+[.、])\s*", "", line).strip()


def push_message(name: str, request: str, final: str, is_error: bool, awaiting: str, kind: str = "",
                 limit_until: float = 0) -> tuple[str, str]:
    """手机通知：标题是哪个对话，正文一句短话说怎么样了（细节点进去看）。"""
    if limit_until:
        import time as _t
        return name, f"额度用完了，{_t.strftime('%H:%M', _t.localtime(limit_until))} 恢复后自动接着做"
    if is_error:
        return name, "出错了，点开看原因"
    if awaiting and kind != "suggest":
        return name, "等你定：" + brief(_plain(awaiting), 18)
    lines = [_plain(ln) for ln in (final or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith(_SECTIONS) and ln != "---"]
    return name, "做完了" + ("：" + brief(lines[0], 18) if lines else "")


def model_name(model_id: str) -> str:
    m = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)-(\d+)", model_id or "")
    return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}" if m else (model_id or "")


def bar(pct: float, n: int = 5) -> str:
    """5 格实心方块进度条；钉钉机器人消息不认字体颜色，靠格数和数字表达。"""
    filled = max(0, min(n, round(pct / 100 * n)))
    if pct > 0 and filled == 0:
        filled = 1 if pct >= 10 else 0
    return f"{'■' * filled}{'□' * (n - filled)} {pct:.0f}%"


def status_line(turn: Turn, limits: dict[str, tuple[float, str | None]]) -> tuple[list[str], list[str]]:
    """底部：一行“模型 · 深度”，下面一张用量小表格（上下文、5 小时、本周）；超过警戒线另起醒目提示。"""
    lines, warns = [], []
    first = [model_name(turn.model)] if turn.model else []
    if turn.effort:
        first.append(f"深度{EFFORT_CN.get(turn.effort, turn.effort)}")
    if first:
        lines.append(" · ".join(first))
    rows = []
    if turn.context_pct is not None:
        rows.append(("上下文", bar(turn.context_pct)))
        if turn.context_pct >= 70:
            warns.append(f"这个对话的上下文已用 {turn.context_pct:.0f}%，快满了，换件新事建议新开对话")
    if turn.compacted:
        warns.append("这一轮里对话自动压缩过一次，前面的细节它可能记得不准")
    keys = [("five_hour", "5 小时"), ("seven_day", "本周")]
    if "fable" in turn.model.lower():
        keys.append(("seven_day_fable", "本周 Fable"))
    for key, short in keys:
        if key in limits:
            util, resets = limits[key]
            rows.append((short, bar(util * 100)))
            if util >= 0.8:
                when = f"，{resets} 重置" if resets else ""
                warns.append(f"{LIMIT_CN[key]}额度已用 {util * 100:.0f}%{when}")
    if rows:
        lines.append("\n".join(["| 用量 | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]))
    return lines, warns


def needs_decision(text: str) -> bool:
    return "要你定" in text[:300]


def _rel(path: str, cwd: str) -> str:
    try:
        rel = os.path.relpath(path, cwd)
        return rel if not rel.startswith("..") else display_path(path)
    except ValueError:
        return display_path(path)


def display_path(path: str) -> str:
    from pathlib import Path
    return display(Path(path))


def to_md(text: str) -> str:
    """钉钉的 Markdown 单个换行不换行，这里统一补成硬换行。"""
    out = []
    for line in text.splitlines():
        out.append(line + "  " if line.strip() else "")
    return "\n".join(out)


def split(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 2 > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts


def build(label: str, turn: Turn, cwd: str, limits: dict | None = None) -> tuple[str, str]:
    minutes = max(1, round((turn.ended - turn.started) / 60))
    files = list(dict.fromkeys(turn.changed_files))
    worked = bool(files or turn.commands or turn.images or turn.files)
    if turn.interrupted:
        title = "停下了"
    elif turn.is_error:
        title = "出错了"
    elif needs_decision(turn.final_text):
        title = "等你定"
    elif worked:
        title = "做完了"
    else:
        title = ""  # 闲聊、直接问答：不加标题，只给回答
    what = turn.request or label

    body = [f"### 【{title}】{what}"] if title else []
    if turn.is_error and turn.error:
        body.append(f"**出错原因**：{turn.error}")
    if turn.final_text:
        body.append(turn.final_text.strip())

    facts = f"#{label.split()[0].lstrip('#')} · {minutes} 分钟"
    if worked:
        facts += f" · 改 {len(files)} 个文件 · 跑 {len(turn.commands)} 条命令"
        if turn.images:
            facts += f" · 截图 {len(turn.images)} 张见下"
        if turn.files:
            facts += f" · 文件 {len(turn.files)} 个见下"
    if turn.injected:
        facts += f" · 中途插话 {turn.injected} 次"
    if turn.translated:
        facts += f" · 英文已由 {turn.translated} 译成中文"
    tail = ["---", facts]
    if turn.reviews:
        tail.append("审核员替你判断：" + "；".join(
            f"{'放行' if r['allow'] else '拦下'} {r['what']}（{r['why']}）" for r in turn.reviews[:6])
            + ("等" if len(turn.reviews) > 6 else ""))
    if turn.links:
        tail.append("　".join(f"[{name}]({url})" for name, url in turn.links))
    if files and "改了哪些" not in turn.final_text:
        shown = [_rel(f, cwd) for f in files[:12]]
        tail.append("改动的文件：" + "、".join(shown) + ("等" if len(files) > 12 else ""))
    status, warns = status_line(turn, limits or {})
    tail += status
    body.append("\n\n".join(tail))
    body += [f"**注意**：{w}" for w in warns]
    return f"{title or '回复'} · {what}", to_md("\n\n".join(body))
