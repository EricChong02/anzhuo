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
    if name.endswith("update_plan"):
        return None
    if name.endswith("send_images"):
        return "交截图"
    if name in ("ToolSearch", "ListAgents", "TaskStop"):
        return None
    return _short(name.split("__")[-1], 22)


def _buttons(buttons: list[dict]) -> str:
    return json.dumps({"msgButtons": buttons}, ensure_ascii=False)


def board_data(items: list[tuple[str, int, Turn | None]], moved: bool = False) -> dict:
    """工作台卡：所有在做、排队的对话一张卡列全，每件带“停”按钮。items 是 (标签, 编号, 这一轮；排队的为 None)。
    moved=True 是被挪到下面后留下的那张旧卡；items 为空表示都做完了。"""
    now = time.strftime("%H:%M")
    if moved:
        return {"title": "工作台", "markdown": "进展挪到下面了 ↓", "tips": "", "sys_full_json_obj": _buttons([])}
    if not items:
        return {"title": "工作台 · 都做完了", "markdown": f"现在没有在做的活（{now}）", "tips": "",
                "sys_full_json_obj": _buttons([])}
    lines, buttons = [], []
    for label, num, turn in items:
        if turn is None:
            lines.append(f"**{_short(label, 18)}**　排队中")
            continue
        mins = int((time.time() - turn.started) // 60)
        last = turn.steps[-1] if turn.steps else "刚开始，正在看情况"
        lines.append(f"**{_short(label, 18)}**　{mins} 分钟 · 第 {len(turn.steps)} 步  \n"
                     f"在做：{_short(turn.request or '看图', 24)}  \n最新：{last}")
        buttons.append({"text": f"停 #{num}", "color": "gray", "id": f"stop:{num}", "request": True})
    running = sum(1 for _, _, t in items if t is not None)
    return {"title": f"工作台 · {running} 件在做", "markdown": "\n\n".join(lines), "tips": f"{now} 更新",
            "sys_full_json_obj": _buttons(buttons)}


@dataclass
class Decision:
    question: str
    options: list[tuple[str, str]]  # (字母, 选项说明)
    recommend: str | None = None


_OPTION = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:方案|选项)?\s*([A-D])\s*(?:\*\*)?\s*[.．、:：)）]\s*(?:\*\*)?\s*(.+)$")
_RECOMMEND = re.compile(r"推荐[^A-D\n]{0,6}([A-D])")


_TABLE_OPTION = re.compile(r"^\s*\|\s*(?:\*\*)?(?:方案|选项)?\s*([A-D])\s*[.．、:：)）]\s*(?:\*\*)?\s*([^|]+)")


def parse_decision(text: str, loose: bool = False) -> Decision | None:
    """从“要你定：……”的回复里拆出问题、选项、推荐；少于两个选项就不做按钮。
    loose=True（网页对话框用）：没写“要你定”、选项写在表格里也认，问题取最后一句问话。"""
    if "要你定" not in text[:300]:
        if not loose:
            return None
        options = []
        for line in text.splitlines():
            if (m := _TABLE_OPTION.match(line) or _OPTION.match(line)) and m.group(1) not in [o[0] for o in options]:
                options.append((m.group(1), m.group(2).strip().strip("*").strip()))
        if len(options) < 2:
            return None
        asks = [ln.strip(" *#") for ln in text.splitlines() if ln.strip(" *").endswith(("？", "?"))]   # “**Q8/12：……？**”这种问句结尾带粗体符号
        rec = _RECOMMEND.search(text)
        recommend = rec.group(1) if rec and rec.group(1) in [o[0] for o in options] else None
        return Decision(_short(asks[-1] if asks else "选哪个？", 60), options[:4], recommend)
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


def confirm_data(label: str, what: str, cmd: str, num: int, result: str = "") -> dict:
    """危险操作等你点头的卡片：允许 / 不允许；点过了显示结果、按钮收起。"""
    md = f"**{what}**  \n要执行：`{_short(cmd, 120)}`"
    if result:
        md += f"  \n\n**{result}**"
    buttons = [] if result else [{"text": "允许", "color": "blue", "id": f"allow:{num}", "request": True},
                                 {"text": "不允许", "color": "gray", "id": f"deny:{num}", "request": True}]
    return {"title": f"等你确认 · {label}", "markdown": md,
            "tips": "" if result else "20 分钟不回就当不允许", "sys_full_json_obj": _buttons(buttons)}


_OUR_ID = re.compile(r"^(stop(?::\d+)?|pick:[A-D]|allow:\d+|deny:\d+)$")


def _strings(x):
    """把回调里所有字符串都摊开（里面套着的 JSON 字符串也拆开）。"""
    if isinstance(x, str):
        t = x.strip()
        if t[:1] in "{[":
            try:
                yield from _strings(json.loads(t))
                return
            except json.JSONDecodeError:
                pass
        yield t
    elif isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def action_of(data: dict | str) -> str | None:
    """按钮回调里找出点的是哪个按钮。钉钉把模板自己的节点名（single_button_node_…）放在 actionIds 里，
    我们设的按钮 id 在别处，所以把整个回调摊开找我们认得的 id；找不到返回 None。"""
    for t in _strings(data):
        if _OUR_ID.match(t):
            return t
    return None
