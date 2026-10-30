"""把钉钉里的一句话解析成指令；不是指令的就当成给对话的话。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_COLON = re.compile(r"[：:]")


@dataclass
class Cmd:
    kind: str  # new / mkdir / list / switch / stop / rename / recent / resume / help / pick / message
    num: int | None = None
    folders: list[str] = field(default_factory=list)
    text: str = ""
    name: str = ""


def _split_head(rest: str) -> tuple[str, str]:
    """“前端 后端：做…” → ("前端 后端", "做…")；没有冒号时整句都是头。"""
    m = _COLON.search(rest)
    if not m:
        return rest.strip(), ""
    return rest[: m.start()].strip(), rest[m.end():].strip()


def _num(s: str) -> int | None:
    m = re.fullmatch(r"#?(\d{1,4})", s.strip())
    return int(m.group(1)) if m else None


def parse(text: str) -> Cmd:
    t = text.strip()
    if t in ("帮助", "用法", "help", "?", "？"):
        return Cmd("help")
    if t in ("列表", "对话", "列表 全部"):
        return Cmd("list", text=t)
    if (n := _num(t)) is not None:
        return Cmd("pick", num=n)
    if t.startswith("新开"):
        head, body = _split_head(t[2:])
        folders = [f for f in re.split(r"[\s+＋、，,]+", head) if f]
        return Cmd("new", folders=folders, text=body)
    if t.startswith("新建"):
        head, body = _split_head(t[2:])
        return Cmd("mkdir", folders=[head] if head else [], text=body)
    if m := re.fullmatch(r"切到\s*(#?\d+)", t):
        return Cmd("switch", num=_num(m.group(1)))
    if m := re.fullmatch(r"停(?:\s*(#?\d+))?", t):
        return Cmd("stop", num=_num(m.group(1)) if m.group(1) else None)
    if m := re.fullmatch(r"(?:删|删除|收起)\s*(#?\d+)", t):
        return Cmd("delete", num=_num(m.group(1)))
    if m := re.fullmatch(r"改名\s*(#?\d+)\s+(.+)", t, re.S):
        return Cmd("rename", num=_num(m.group(1)), name=m.group(2).strip())
    if m := re.fullmatch(r"压缩(?:上下文)?(?:\s*(#?\d+))?", t):
        return Cmd("compact", num=_num(m.group(1)) if m.group(1) else None)
    if t in ("网页", "登录", "过程"):
        return Cmd("web")
    if t in ("额度", "用量"):
        return Cmd("quota")
    if m := re.fullmatch(r"(模型|深度)(?:\s*(#?\d+))?(?:\s+(\S+))?", t):
        return Cmd("model" if m.group(1) == "模型" else "effort",
                   num=_num(m.group(2)) if m.group(2) else None, name=m.group(3) or "")
    if m := re.fullmatch(r"最近\s+(\S+)", t):
        return Cmd("recent", folders=[m.group(1)])
    if m := re.fullmatch(r"接着桌面(?:\s*(\d+))?", t):
        return Cmd("desktop", num=int(m.group(1)) if m.group(1) else None)
    if m := re.fullmatch(r"接着\s+(\S+)\s+(\d+)", t):
        return Cmd("resume", folders=[m.group(1)], num=int(m.group(2)))
    if m := re.match(r"#(\d{1,4})\s+(.+)", t, re.S):
        return Cmd("message", num=int(m.group(1)), text=m.group(2).strip())
    return Cmd("message", text=t)


HELP = """**案头用法**

- 直接说话就行：话交给当前对话；它正在做事时，就是中途插话
- `新开 文件夹：要做的事`：新开对话。文件夹可以写配置里的短名、完整路径，或文件夹名里的几个字；不写文件夹就用临时文件夹
- `列表`　看对话；`切到 7`　换当前对话；`#7 要说的话`　点名交给 #7；`停 7`　让 #7 停下
- `模型 sonnet`、`深度 高`（后面可加编号）：换模型、换思考深度；`压缩`：压缩上下文；`额度`：查用量
- `接着桌面`：接上你最近在电脑桌面端聊的那段对话
- 定时任务可直接说“明早8点整理全球要闻”“每周五下午3点汇总周报”“两小时后接着这个对话做”

看进度、回到某条回复、换模型、改名、管理定时任务都在案头网页里做（发“网页”拿链接）。
"""
