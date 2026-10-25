"""钉钉卡片：干活时的进度卡（同一张卡原地更新，带“停下”按钮）和“要你定”按钮卡（点一下就把选择交回对话）。"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .report import Turn

SHOW_STEPS = 6  # 进度卡只列最近几步，手机一屏看得完


def _short(text: str, n: int = 22) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def step_label(name: str, inp: dict) -> str | None:
    """把一次工具调用说成一句人话；不值得给你看的返回 None。"""
    def fname(key: str = "file_path") -> str:
        return Path(str(inp.get(key) or inp.get("notebook_path") or "")).name or "文件"

    if name == "Read":
        return f"看 {fname()}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"改 {fname()}"
    if name == "Write":
        return f"写 {fname()}"
    if name == "Bash":
        return "跑命令：" + _short(inp.get("description") or inp.get("command") or "", 18)
    if name in ("Grep", "Glob"):
        return "搜：" + _short(inp.get("pattern") or "", 18)
    if name == "WebSearch":
        return "上网查：" + _short(inp.get("query") or "", 18)
    if name == "WebFetch":
        return "读网页"
    if name in ("Task", "Agent"):
        return "派助手：" + _short(inp.get("description") or "", 18)
    if name == "Skill":
        return "按流程：" + _short(inp.get("skill") or "", 18)
    if name.endswith("send_images"):
        return "交截图"
    if name in ("ToolSearch", "ListAgents", "TaskStop"):
        return None
    return _short(name.split("__")[-1], 22)


def _buttons(buttons: list[dict]) -> str:
    return json.dumps({"msgButtons": buttons}, ensure_ascii=False)


def progress_data(label: str, turn: Turn, final: str = "") -> dict:
    """进度卡的内容。final 为空表示还在做；否则是结束时的标题（做完了 / 出错了 / 停下了 / 等你定）。"""
    mins = max(0, int(((turn.ended or time.time()) - turn.started) // 60))
    steps = turn.steps[-SHOW_STEPS:]
    first = len(turn.steps) - len(steps) + 1
    lines = [f"**{turn.request or label}**"]
    if final:
        lines.append(f"共 {len(turn.steps)} 步，用时 {max(1, mins)} 分钟，结果见下面的报告。")
    elif steps:
        lines += [f"{first + i}. {s}" for i, s in enumerate(steps)]
    else:
        lines.append("刚开始，正在看情况……")
    tips = "" if final else f"已用 {mins} 分钟 · {time.strftime('%H:%M')} 更新"
    stop = [] if final else [{"text": "停下", "color": "gray", "id": "stop", "request": True}]
    return {"title": f"{final or '在做'} · {label}", "markdown": "  \n".join(lines), "tips": tips,
            "sys_full_json_obj": _buttons(stop)}


@dataclass
class Decision:
    question: str
    options: list[tuple[str, str]]  # (字母, 选项说明)
    recommend: str | None = None


_OPTION = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:方案|选项)?\s*([A-D])\s*(?:\*\*)?\s*[.．、:：)）]\s*(?:\*\*)?\s*(.+)$")
_RECOMMEND = re.compile(r"推荐[^A-D\n]{0,6}([A-D])")


def parse_decision(text: str) -> Decision | None:
    """从“要你定：……”的回复里拆出问题、选项、推荐；少于两个选项就不做按钮。"""
    if "要你定" not in text[:300]:
        return None
    after = text[text.index("要你定"):]
    question = re.sub(r"^要你定[\*：:\s]*", "", after.splitlines()[0]).strip(" *") or "要你定"
    options: list[tuple[str, str]] = []
    for line in after.splitlines()[1:]:
        if m := _OPTION.match(line):
            letter, desc = m.group(1), m.group(2).strip().strip("*").strip()
            if letter not in [o[0] for o in options]:
                options.append((letter, desc))
    if len(options) < 2:
        return None
    rec = _RECOMMEND.search(after)
    recommend = rec.group(1) if rec and rec.group(1) in [o[0] for o in options] else None
    return Decision(_short(question, 60), options[:4], recommend)


def decision_data(label: str, d: Decision, picked: str | None = None) -> dict:
    lines = [f"**{d.question}**"] + [f"**{k}**　{_short(v, 60)}" for k, v in d.options]
    if d.recommend:
        lines.append(f"推荐 {d.recommend}")
    if picked:
        lines.append(f"**你选了 {picked}**，已交给 {label}")
    buttons = [] if picked else [
        {"text": f"选 {k}" + ("（推荐）" if k == d.recommend else ""), "color": "blue" if k == d.recommend else "gray",
         "id": f"pick:{k}", "request": True} for k, _ in d.options]
    return {"title": f"{'已选 ' + picked if picked else '要你定'} · {label}", "markdown": "  \n".join(lines),
            "tips": "" if picked else "点按钮，或直接回字说别的想法",
            "sys_full_json_obj": _buttons(buttons)}


def action_of(content: dict | str) -> str | None:
    """按钮回调里找出点的是哪个按钮（id）。钉钉回调结构不完全固定，按已知的几处找。"""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    private = content.get("cardPrivateData") or {}
    ids = private.get("actionIds") or content.get("actionIds") or []
    if ids:
        return str(ids[0])
    m = re.search(r'"(stop|pick:[A-D])"', json.dumps(content))
    return m.group(1) if m else None
