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
    """“课程根 平台：做…” → ("课程根 平台", "做…")；没有冒号时整句都是头。"""
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


HELP = """**小E 用法**

- `新开 课程根：要做的事` 在某个文件夹开一个新对话（短名、完整路径、文件夹名的一部分都行）
- `新开 课程根 平台：…` 同时用几个文件夹，第一个为主
- `新开：…` 不指定文件夹，用一个临时文件夹
- `新建 personal/菜谱整理：…` 先建文件夹再开工
- 对话正在做时也能直接说：话会立刻插给它，不用等它做完
- 直接说话：交给当前对话；引用某条消息再说：交给那条消息的对话；`#7 …` 交给 #7
- `列表` 看对话（7 天没动的自动收起，`列表 全部` 连收起的一起看）　`切到 7` 换当前对话　`停 7` 让 #7 停下　`改名 7 新名字`
- `删 7` 收起 #7：不再出现在列表里，小E 也不再把话交给它；聊天记录还在，发 `#7 …` 就接着聊
- `模型 sonnet` / `模型 7 opus` 换当前或 #7 的模型（opus、sonnet、haiku、fable、默认）　`深度 高`（低、中、高、很高、最高、默认）
- `额度` 看 5 小时和本周额度用了多少
- `压缩` / `压缩 7` 压缩当前或 #7 的上下文（正在做就等这一轮做完再压）
- `网页` 拿一个进门链接：看最近每一轮的完整过程，改过课的能直接试课
- 有活在做时，聊天最下面始终有一张“工作台”卡，列出每件在做哪一步，可以点“停 #N”；要你定的事会出按钮卡，点一下就交回对话
- `接着桌面` 接上你最近在桌面端聊的那段（接上后桌面端那边先别再说话）
- `最近 课程根` 看这个文件夹里最近的对话（含桌面端开的）　`接着 课程根 2` 接着其中第 2 个聊
"""
