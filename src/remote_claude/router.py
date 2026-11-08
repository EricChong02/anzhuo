"""你直接说话时，用最便宜的模型判断：接着哪个对话，还是新开一个、在哪个文件夹。拿不准就让小E问你。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from .commands import HELP
from .folders import display
from .sessions import Session
from . import split
from .engines import HELPER_MODEL

log = logging.getLogger(__name__)

ROUTER_MODEL = HELPER_MODEL
KINDS = tuple(split.KINDS)  # 新事的三档：拿主意定方案 / 照要求开发 / 其余；各交给谁见分工表（split.py，配置 [split]）
ROUTER_EFFORT = "low"  # 小E 自己的判断只用低档思考；干活的对话另按配置（默认中）

SYSTEM = """你是一个分派员。用户在手机上给他 Mac 上的几个工作对话发消息，你判断这条消息该交给谁。
只输出一个 JSON 对象，不要任何别的文字：
{"action": "reply" | "continue" | "new" | "ask", "target": 对话编号或 null, "folder": 文件夹或 null, "kind": "design" | "dev" | "simple" | null, "answer": 文字或 null, "reason": "十个字以内"}
- reply：只限打招呼、谢谢、问你是谁、问远窗的指令怎么写，或和所有对话都毫不相关的闲聊、常识问答；你直接回答。answer 写给用户的回答，简体中文，口语，一到三句话；问用法时按下面的用法说明答。
  你只是分派员，不能替干活的对话回答它的事，也不要用它的口吻道歉、解释或承诺。
  问“以后能不能…”“开发完之后…”“要怎么设计”这类关于功能本身的问题，不是问指令怎么写：如果有对话在开发远窗（原名案头、小E，remote-claude），选 continue 它。
- continue：消息是在接着某个已有对话说（追问、补充、修改意见、“继续”、“好的”、“选B”、对刚做完的事提要求等）。target 写那个对话的编号；多半是“当前对话”。
- new：消息是一件和已有对话都不相干的新事。folder 写最合适的文件夹：优先用给出的短名（每个短名后面有它放什么的说明，按说明选，不要只看名字字面）；否则写用户提到的文件夹名或其中几个字；完全不涉及文件夹（查资料、算东西、闲聊）就写 null。
  涉及具体的人、课表、排课、学生、老师、学校工作、财务、健康、个人事务的问题，这些资料在 Projects（庄毅的整个工作区），不在课程根或平台，folder 选 Projects。课程根只给做课件和课程页面用，平台只给改 CyberScope 平台代码用。
- ask：确实拿不准是接着还是新开，或看不出是接哪个对话。
判断主要看话题：消息和哪个已有对话的内容相关（同一个文件夹、同一门课、同一件事），就 continue 那个对话，不一定是当前对话。
只有像是对上一条回答的直接回复（“好的”“选B”“继续”“再大一点”“为什么”这类，看不出话题），才 continue 当前对话。
打招呼、闲聊这类不是任务，不要 new；要用到某个对话内容的（“刚才为什么这么改”“上面那个再解释下”）选 continue。
只有用户要办一件和所有已有对话都不相关的事，才选 new。
有对话“正在做”或几分钟前刚做完时，用户的话多半是说给它的：讨论它在做的东西、对它发来的卡片/报告/截图的反馈、抱怨、追问、出主意，都选 continue 它。拿不准是 reply 还是 continue 时，选 continue。
标着“在等你回答”的对话刚问了用户问题；用户的话像是在回答它（选方案、同意、补充意见），就 continue 它，即使它不是当前对话。
如果给了“你刚才直接回答用户的”，而这条消息是在接着那段话说（追问、纠正、补充，比如“她老公不是老师”“那如果是…呢”），就选 reply，结合前文接着回答；那段话要用到文件或动手做事时才选 new。
选 new 时再给 kind：要拿主意、定方案的写 "design"——设计一节课或整门课、整节课重做、定课件结构和交互、梳理想法、写设计方案、跨多个文件的架构设计、验收别人做完的活；
照着已经清楚的要求动手开发的写 "dev"——新做一个功能或页面、跨多个文件的改动、重构、难查的 bug、照定好的方案实现课件某一节；
其余都写 "simple"：查资料、整理文件、问答、写一段文字、算东西，以及小开发（改样式或文案、调个参数或配置、改一两处小 bug、写个小脚本、跑命令、看代码解释代码、单文件小改动）。凡是“加一个功能”“前后端都要改”这类，哪怕话很短也至少写 "dev"。其他情况 kind 写 null。"""

KIND_SYSTEM = """判断用户这件事该用哪档模型。只输出一个词，不要别的文字：
design：要拿主意、定方案——设计一节课或整门课、整节课重做、定课件结构和交互、梳理想法、写设计方案、跨多个文件的架构设计、验收别人做完的活。
dev：照着已经清楚的要求动手开发——新做功能或页面、跨多个文件的改动、重构、难查的 bug、照定好的方案实现课件某一节。
simple：其余所有事——查资料、整理文件、问答、写文字，以及小开发（改样式或文案、调参数或配置、改一两处小 bug、写小脚本、跑命令、看代码解释代码、单文件小改动）。
凡是“加一个功能”“做一个页面”“前后端都要改”“重构”，至少算 dev；“设计方案”“这节课怎么设计”“帮我理一理”“整节课重做”算 design；只有一两处小改动才算 simple。
例子——simple：“把按钮颜色改成蓝色”“把超时从30秒改成60秒”“修一下错别字”“这个报错怎么回事”“这个函数干嘛的”；dev：“给小E加个定时重试机制”“按方案把第三节的互动做出来”“重构路由模块”；design：“设计一节二次函数的课”“这个平台的权限怎么设计”“帮我理理这学期的教研想法”。"""


@dataclass
class Route:
    action: str  # continue / new / ask
    target: int | None = None
    folder: str | None = None
    reason: str = ""
    kind: str | None = None  # 新事是定方案（design）、开发（dev）还是简单任务（simple）
    answer: str = ""  # action 为 reply 时小E直接回的话


FOLDER_HINTS = {
    "课程根": "只放 CyberScope 的课件和课程页面，做课件、改课程页才选它；学生名单、课表、成绩都不在这里",
    "平台": "CyberScope 平台的代码仓，只在改平台代码时选",
    "Projects": "庄毅的整个工作区：人物、课表、排课、学校工作、财务、健康、个人事务、各个项目；拿不准在哪时选它",
    "下载": "下载文件夹，只在用户说要处理下载的文件时选",
}


def _hint(name: str) -> str:
    return f"{name}（{FOLDER_HINTS[name]}）" if name in FOLDER_HINTS else name


def _context(text: str, sessions: list[Session], current: int | None, shortnames: list[str],
             chat: list[tuple[str, str]] | None = None, busy: set[int] | None = None) -> str:
    rows = []
    for s in sorted(sessions, key=lambda x: -x.updated)[:8]:
        mins = int((time.time() - s.updated) / 60)
        cur = "（当前对话）" if s.num == current else ""
        when = "正在做" if s.num in (busy or set()) else f"{mins} 分钟前"
        if s.awaiting:
            when += f"｜在等你回答：{s.awaiting}"
        last = f"｜上一轮：{s.last_summary}" if s.last_summary else (f"｜上次：{s.last_title}" if s.last_title else "")
        rows.append(f"- #{s.num}{cur} {s.name}｜文件夹 {display(Path(s.cwd))}｜{when}{last}")
    said = "".join(f"\n用户：{q[:300]}\n你：{a[:400]}" for q, a in chat or [])
    return (
        (f"你刚才直接回答用户的（从早到晚）：{said}\n\n" if said else "")
        + "已有对话：\n" + ("\n".join(rows) if rows else "（没有）")
        + f"\n\n文件夹短名（括号里是放什么）：{'、'.join(_hint(n) for n in shortnames)}\n\n用户这条消息：\n{text[:1500]}"
    )


def _parse(raw: str, valid: set[int]) -> Route | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    action = j.get("action")
    if action not in ("reply", "continue", "new", "ask"):
        return None
    target = j.get("target")
    target = int(target) if isinstance(target, (int, str)) and str(target).lstrip("#").isdigit() else None
    if isinstance(target, int) and target not in valid:
        target = None
    if action == "continue" and target is None:
        action = "ask"
    folder = j.get("folder")
    folder = str(folder).strip() if folder not in (None, "", "null") else None
    kind = j.get("kind") if j.get("kind") in KINDS else None
    answer = str(j.get("answer") or "").strip()
    if action == "reply" and not answer:
        action = "continue" if target else "ask"
    return Route(action, target, folder, str(j.get("reason") or "")[:20], kind, answer[:400])


async def route(text: str, sessions: list[Session], current: int | None, shortnames: list[str],
                timeout: float = 25, chat: list[tuple[str, str]] | None = None,
                busy: set[int] | None = None) -> Route | None:
    """返回判断结果；出错、超时、答非所问时返回 None，由调用方按老规矩交给当前对话。"""
    options = ClaudeAgentOptions(
        model=ROUTER_MODEL, effort=ROUTER_EFFORT, system_prompt=SYSTEM + "\n\n你叫远窗（原名案头、小E），是用户钉钉里的助手，背后是用户 Mac 上的 Claude Code。称呼用户时用“你”。用法说明：\n" + HELP,
        tools=[], max_turns=1,
        setting_sources=[], cwd=str(Path.home()),
        extra_args={"no-session-persistence": None},  # 不落盘，免得混进“最近的对话”
    )
    out = []

    async def run():
        async for msg in query(prompt=_context(text, sessions, current, shortnames, chat, busy), options=options):
            if isinstance(msg, AssistantMessage):
                out.extend(b.text for b in msg.content if isinstance(b, TextBlock))

    try:
        await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001 - 判断失败就退回老规矩
        log.warning("自动判断失败", exc_info=True)
        return None
    r = _parse("".join(out), {s.num for s in sessions})
    log.info("自动判断：%s", r)
    return r


async def classify_kind(text: str, timeout: float = 15) -> str | None:
    """只判断这件事是定方案（design）、开发（dev）还是简单任务（simple），挑模型用；出错、超时返回 None，由调用方按老规矩。"""
    options = ClaudeAgentOptions(model=ROUTER_MODEL, effort=ROUTER_EFFORT, system_prompt=KIND_SYSTEM, tools=[], max_turns=1,
                                 setting_sources=[], cwd=str(Path.home()), extra_args={"no-session-persistence": None})
    out = []

    async def run():
        async for msg in query(prompt=text[:1500], options=options):
            if isinstance(msg, AssistantMessage):
                out.extend(b.text for b in msg.content if isinstance(b, TextBlock))

    try:
        await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001
        log.warning("判断任务轻重失败", exc_info=True)
        return None
    word = "".join(out).strip().lower()
    return word if word in KINDS else None
