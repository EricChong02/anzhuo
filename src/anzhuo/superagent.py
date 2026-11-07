"""总管（super_agent，默认关闭）：你只跟一个对话说话，它替你开新对话派活、转达后续的话、汇总回报。

照着 DeepSeek 的 super-agent 原型（dsh-superagent-prototype）做：
- 总管只路由，不亲自干活（改文件、跑命令的工具用钩子拒掉）；派活交出用户原话。
- 派出去的对话（worker）用 report 工具结构化汇报：ready / waiting / blocked / failed / dropped，
  要用户拍板时带一个 question（human_action），一问出来就转给总管，不等这一轮结束。
- worker 觉得一句话不属于自己这件事，用 handback 退回给总管重新分派。
- 订阅直投：你在总管里写“#N 话”，直接交给 #N，不用总管跑一轮；总管下次开口前会看到这件事。
- 你直接在 worker 里说话：照旧直接通知你，同时给总管记一笔（不让它跑一轮）。
- 总管有自己的并行名额；远窗重启时被打断的派出去的活，带恢复提示重新开工。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import tool

from . import folders
from .dingtalk import Incoming
from .sessions import Session

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger(__name__)

ROUTER = "router"
NAME = "总管"
VIA = "router"   # 总管派的活（包括你在总管里用 #N 直接交给某个对话的话）
REPORT = "report"   # 给总管的回报、退回、提问
HUMAN = ("web", "dingtalk")   # 你自己说的话
STATES = ("ready", "waiting", "blocked", "failed", "dropped")
STATE_CN = {"ready": "做完了", "waiting": "这轮做完，等后续", "blocked": "卡住了", "failed": "做不成", "dropped": "不用做",
            "progress": "在做", "stopped": "被停下了", "error": "出错了", "asked": "在等用户定"}
NOTES_MAX = 20   # 总管下次开口前要看的“期间的情况”最多攒几条
REPORT_MAX = 4000   # 回报正文最多带多少字；再长总管用 session_status 看
TAGS = ("【回报】", "【退回】", "【要用户定】", "【重启】")   # 给总管的消息；网页里折成卡片

RULES = """
你是“总管”。用户只跟你说话，你替用户管其他对话：开新对话派活、把后续的话转给对应的对话、汇总回报。
- 你只路由，不亲自干活：不改文件、不跑命令、不上网查、不替对话设计方案或拆步骤。真正的活交给对话去做。
- 派活时把用户的原话原样交出去（可以在前面加一句不超过 30 字的背景，比如“接着 #12 的结果”），不要改写成你自己的计划。
- 先用 list_sessions 看看有没有正在做同一件事的对话：用户明确指着某个对话（说了编号、说“刚才那个”且只对得上一个）就用 send_to_session 交给它；只是话题相近，算新的事，用 new_session 新开。
- 用户点名用哪个引擎（Claude / GPT）或模型就照办；没点名就不填，用默认。
- 派完就结束这一轮，用一句话告诉用户交给了谁（#编号）。不要等、不要反复查状态：对话做完、要用户定、出错时会自动给你发消息。
- 【回报】：做完了就用一两句话告诉用户结论和去哪看全文（#编号），不要整段复述、不要重新排版它的列表；卡住或做不成就说清楚卡在哪、要用户做什么。同一件事已经说过的进展不要重复说。
- 【要用户定】：把对话的问题原样转给用户（选项、推荐都带上），一次只问一个。
- 【退回】：某个对话说这句话不属于它那件事。重新判断：另开新对话、交给别的对话，或者问用户；不要再交回给退回它的那个对话。
- 【期间的情况】：你上次说话之后发生的事（用户直接跟某个对话说了什么、对话的进展）。只当背景，已经告诉过用户的不要再说。
- 回报里的密码、密钥、令牌、验证码不要写进你的回复，只说“在 #编号 的回复里”。
- 用户回答了某个对话的问题，用 send_to_session 把用户的原话转过去。用户也可以自己写“#编号 话”直接交给那个对话，那种不会经过你。
- 用户问“现在都在干嘛”，用 list_sessions 汇总，只说需要用户关心的。
"""

WORKER_RULES = """
这件事是“总管”派给你的，用户通过总管跟你沟通。按下面的约定汇报：
- 这一轮结束前调用一次 mcp__anzhuo__report：state 选 ready（做完了，有结果能看、能用）、waiting（这轮做完了，等后续消息再接着做）、blocked（卡住了，要用户给东西、权限或决定才能继续）、failed（做不成）、dropped（不用做：已经有人做了、问题不成立）；summary 一两句话写结论。
- 要用户拍板时，在 report 里填 question：一个具体问题，可带 A/B/C 选项和推荐；state 用 waiting。一次只问一个。问完能先做的照做。
- “这件事”就是最开始交给你的那件活。之后收到的话：是对这件事的修改、补充、追问、对你问题的回答，就接着做；是一件和它无关的新任务（哪怕是点名交给你的），调用 mcp__anzhuo__handback 写明原因，然后直接结束这一轮，不要回答它、不要去做——总管会另开对话去做。
"""

# 总管不亲自干活：改文件、跑命令、上网查、派子助手都不行（读文件、找文件夹还可以）
DISALLOWED = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput", "Task", "Agent", "WebSearch", "WebFetch",
              "mcp__anzhuo__generate_images", "mcp__anzhuo__send_files", "mcp__anzhuo__send_images"]
BUILTIN = ["Read", "Glob", "Grep"]   # 总管能看到的内置工具：只能读、找
DENY_REASON = "你是总管，不亲自干活。用 new_session 开个对话去做，或用 send_to_session 交给已有的对话。"
RECOVER = ("（远窗重启了，打断了你上一轮。先看看工作文件夹、git 状态和你已经做过的外部操作（提交、推送、发消息），"
           "确认做到哪了，做过的别再做一遍，然后接着做完；最后照约定调用 report。）")
FOLLOW = "\n\n（经总管转来。先对照你这件事判断：属于这件事就接着做，最后调用 report；是和它无关的新任务就调用 handback 退回，不要去做。）"
NUDGE = ("结束前还没汇报。属于这件事：调用 mcp__anzhuo__report（state + 一两句 summary，要用户拍板就填 question）；"
         "这句话不属于这件事：调用 mcp__anzhuo__handback。调用后再结束。")


def must_report(turn, nudged: bool) -> str:
    """总管派的这一轮要结束了却没汇报也没退回：拦一次，让它补上。"""
    if nudged or turn.via != VIA or getattr(turn, "sa_report", None) or getattr(turn, "sa_handback", ""):
        return ""
    return NUDGE


DIRECT = re.compile(r"^\s*#(\d{1,5})\s*[:：,，]?\s*(.+)$", re.S)


def folder_hint(bridge: "Bridge") -> str:
    short = "、".join(f"{k}（{v}）" for k, v in bridge.cfg.folders.items())
    return ("- 派活的文件夹：用户说了文件夹，folder 就填用户说的那个词或完整路径" + (f"；用户的文件夹短名有：{short}" if short else "")
            + "。用户没说文件夹，就不填 folder（会用新的临时文件夹）。不要把你自己的工作文件夹填给别的对话。\n")


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)}]}


def _error(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _cut(text: str, limit: int = REPORT_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n\n（后面还有，太长截掉了；要看全文用 session_status）"


def state(bridge: "Bridge", s: Session) -> str:
    if bridge.runner.busy(s.num):
        return "在做" if s.num in bridge.runner.turns else "排队"
    if s.awaiting and s.awaiting_kind == "decide":
        return "等用户定"
    if s.status == "error":
        return "出错"
    if s.ticket:
        return STATE_CN.get(s.ticket, s.ticket)
    return "做完了（顺口问要不要再做点什么）" if s.awaiting else "空闲"


def brief(bridge: "Bridge", s: Session) -> dict:
    return {"num": s.num, "name": s.name, "folder": folders.display(Path(s.cwd)), "state": state(bridge, s),
            "engine": "GPT" if s.engine == "codex" else "Claude", "awaiting": s.awaiting,
            "last": s.last_summary, "from_router": bool(s.boss)}


def find(bridge: "Bridge", owner: str = "admin") -> Session | None:
    return next((s for s in bridge.registry.sessions.values() if s.role == ROUTER and s.owner == owner and not s.hidden), None)


def boss_of(bridge: "Bridge", s: Session) -> Session | None:
    r = bridge.registry.get(s.boss) if s.boss else None
    return r if r and r.role == ROUTER and not r.hidden and bridge.cfg.super_agent else None


def ensure(bridge: "Bridge") -> Session:
    """打开 super_agent 时保证有一个总管对话（置顶、固定用 Claude、在自己的文件夹里）。"""
    if s := find(bridge):
        return s
    from .app import MODELS
    num = bridge.registry.reserve_num()
    cwd = bridge.data_dir / "router"
    cwd.mkdir(parents=True, exist_ok=True)
    s = Session(num=num, name=NAME, cwd=str(cwd), model=MODELS.get(bridge.cfg.simple_model, bridge.cfg.simple_model),
                role=ROUTER, pinned=True, renamed=True, titled=True, worked=True)
    bridge.registry.add(s)
    bridge.registry.save()
    log.info("总管已开好：#%s", num)
    return s


def last_reply(s: Session) -> str:
    """远窗重启后内存里没有上一轮了：从 Claude 的会话记录里找最后一条回复；找不到就用列表里那句摘要。"""
    if s.claude_id:
        try:
            from . import thread
            items = thread.history(thread.full_messages(s.claude_id, s.cwd))
            if said := next((i["text"] for i in reversed(items) if i.get("who") == "claude"), ""):
                return said
        except Exception:  # noqa: BLE001 - 记录读不了就退回摘要
            log.debug("读 #%s 的记录失败", s.num, exc_info=True)
    return s.last_summary


def role(bridge: "Bridge", s: Session | None) -> dict | None:
    if not (bridge.cfg.super_agent and s):
        return None
    if s.role == ROUTER:
        return {"tools": tools(bridge, s), "append": RULES + folder_hint(bridge), "disallowed": DISALLOWED, "builtin": BUILTIN,
                "deny_reason": DENY_REASON, "own_slot": True}
    if boss_of(bridge, s):
        return {"tools": worker_tools(bridge, s), "append": WORKER_RULES, "on_stop": must_report}
    return None


def _incoming(text: str, via: str) -> Incoming:
    return Incoming(sender_id="local", sender_nick=NAME, conversation_type="1", kind="text", text=text, via=via)


# ---------- 给总管记一笔（不让它跑一轮），它下次开口前一起看 ----------

def note(router: Session, text: str) -> None:
    router.notes = (router.notes + [f"{time.strftime('%H:%M')} {text}"])[-NOTES_MAX:]


def take_notes(router: Session, text: str) -> str:
    """总管要开口了：把攒着的“期间的情况”放在这句话前面，然后清空。"""
    if not router.notes:
        return text
    notes, router.notes = router.notes, []
    return "【期间的情况】\n" + "\n".join(f"- {n}" for n in notes) + "\n\n" + text


async def tell(bridge: "Bridge", router: Session, text: str) -> None:
    """让总管跑一轮看这条消息（回报、退回、提问）。"""
    await bridge._submit(router, text, _incoming("", REPORT))


async def direct(bridge: "Bridge", router: Session, text: str) -> bool:
    """订阅直投：你在总管里写“#N 话”，直接交给 #N，不经过总管的判断。"""
    m = DIRECT.match(text or "")
    if not m:
        return False
    s = bridge.registry.mine(int(m.group(1)), router.owner)
    if not s or s.role == ROUTER:
        return False
    said = m.group(2).strip()
    s.boss, s.hidden = router.num, False
    note(router, f"用户直接对 #{s.num} {s.name} 说：{said[:200]}")
    bridge.registry.save()
    await bridge._submit(s, said + FOLLOW, _incoming(said, VIA))
    return True


# ---------- 总管的工具 ----------

def tools(bridge: "Bridge", router: Session) -> list:
    reg = bridge.registry

    def worker(num) -> Session | None:
        try:
            s = reg.mine(int(num), router.owner)
        except (TypeError, ValueError):
            return None
        return None if not s or s.role == ROUTER else s

    @tool("list_sessions", "列出用户的对话（不含你自己）：编号、名字、文件夹、状态、在等用户定什么、上一轮说了什么。"
          "默认只列最近的 20 个。",
          {"type": "object", "properties": {"limit": {"type": "integer"}}})
    async def list_sessions(args):
        items = sorted((x for x in reg.visible(router.owner) if x.role != ROUTER and not x.hidden), key=lambda x: -x.updated)
        limit = max(1, min(int(args.get("limit") or 20), 50))
        return _text([brief(bridge, x) for x in items[:limit]])

    @tool("new_session", "开一个新对话去做一件事，立刻开工。task 写用户的原话（可加一句背景）；"
          "folder 可写文件夹短名、完整路径或文件夹名里的几个字，不写就用临时文件夹；"
          "engine 只在用户点名时填：claude 或 gpt；model 只在用户点名时填：opus / sonnet / haiku / fable。",
          {"type": "object", "properties": {"task": {"type": "string"}, "folder": {"type": "string"},
                                            "engine": {"type": "string", "enum": ["claude", "gpt"]},
                                            "model": {"type": "string"}}, "required": ["task"]})
    async def new_session(args):
        from .app import MODELS
        task = str(args.get("task") or "").strip()
        if not task:
            return _error("task 不能为空")
        spec = str(args.get("folder") or "").strip()
        cwd = None
        if spec:
            cands = folders.candidates(spec, bridge.cfg)
            if not cands:
                return _error(f"没找到文件夹“{spec}”。可以写短名（{'、'.join(bridge.cfg.folders) or '无'}）、完整路径，或不写用临时文件夹。")
            if len(cands) > 1:
                return _error("文件夹有几个对得上，问用户选哪个，或换成完整路径：\n" + "\n".join(str(p) for p in cands[:8]))
            if cands[0].resolve() != Path(router.cwd).resolve():   # 总管自己的文件夹不给别的对话用
                cwd = cands[0]
                if spec != str(cwd):   # 用户说的是短名 / 名字：告诉它说的就是当前文件夹，免得在里面再建一个同名的
                    task = f"（用户说的“{spec}”就是你现在的工作文件夹 {cwd}）\n{task}"
        model = "gpt" if args.get("engine") == "gpt" else MODELS.get(str(args.get("model") or "").lower())
        num = reg.reserve_num()
        if cwd is None:
            cwd = folders.scratch_dir(bridge.cfg, num)
        await bridge._start(num, cwd, [], task, _incoming(task, VIA), model=model, owner=router.owner, boss=router.num)
        s = reg.get(num)
        if not s:
            return _error("没开成")
        return _text(f"已开 #{s.num} {s.name}，在 {folders.display(Path(s.cwd))} 开工了。做完、要用户定或出错时会告诉你。")

    @tool("send_to_session", "把一句话交给某个已有对话（用户的后续要求、对它问题的回答）。它正在做就插话，没在做就开始新一轮。",
          {"type": "object", "properties": {"num": {"type": "integer"}, "text": {"type": "string"}}, "required": ["num", "text"]})
    async def send_to_session(args):
        s = worker(args.get("num"))
        text = str(args.get("text") or "").strip()
        if not s:
            return _error(f"没有 #{args.get('num')} 这个对话")
        if not text:
            return _error("text 不能为空")
        if s.num in getattr(router, "_handed", {}).get(text[:80], set()):
            return _error(f"#{s.num} 刚把这句话退回来了，说它不属于那件事。换个对话或新开一个。")
        s.boss, s.hidden = router.num, False   # 之后它做完这件也告诉总管
        await bridge._submit(s, text + FOLLOW, _incoming(text, VIA))
        return _text(f"已交给 #{s.num} {s.name}。")

    @tool("session_status", "看某个对话的现状和它最后一次的完整回复。只在用户问起、或回报里信息不够时用。",
          {"type": "object", "properties": {"num": {"type": "integer"}}, "required": ["num"]})
    async def session_status(args):
        s = worker(args.get("num"))
        if not s:
            return _error(f"没有 #{args.get('num')} 这个对话")
        info = brief(bridge, s)
        last = bridge.runner.last.get(s.num)
        info["reply"] = _cut(last.final_text if last else last_reply(s), 12000)
        return _text(info)

    @tool("stop_session", "让某个对话停下（用户要求停时才用）。",
          {"type": "object", "properties": {"num": {"type": "integer"}}, "required": ["num"]})
    async def stop_session(args):
        s = worker(args.get("num"))
        if not s:
            return _error(f"没有 #{args.get('num')} 这个对话")
        stopped = await bridge.runner.stop(s.num)
        s.ticket = "stopped"
        return _text(f"#{s.num} {'已让它停下' if stopped else '本来就没在做，排队的也清掉了'}。")

    return [list_sessions, new_session, send_to_session, session_status, stop_session]


# ---------- worker 的工具：结构化汇报、退回 ----------

def worker_tools(bridge: "Bridge", s: Session) -> list:
    def turn():
        return bridge.runner.turns.get(s.num)

    @tool("report", "向总管汇报这件事的状态（每一轮结束前调用一次）。state：ready 做完了 / waiting 这轮做完等后续 / "
          "blocked 卡住了要用户给东西 / failed 做不成 / dropped 不用做。summary 一两句话写结论。"
          "要用户拍板时填 question（一个具体问题，可带 A/B/C 选项和推荐），state 用 waiting。",
          {"type": "object", "properties": {"state": {"type": "string", "enum": list(STATES)},
                                            "summary": {"type": "string"}, "question": {"type": "string"}},
           "required": ["state", "summary"]})
    async def report(args):
        t = turn()
        st = args.get("state") if args.get("state") in STATES else "ready"
        rep = {"state": st, "summary": str(args.get("summary") or "")[:1000], "question": str(args.get("question") or "").strip()[:2000]}
        if t is not None:
            t.sa_report = rep
        if rep["question"]:
            s.awaiting, s.awaiting_kind = rep["question"].splitlines()[0][:200], "decide"
            router = boss_of(bridge, s)
            # 要用户定的事马上转给总管（不等这一轮结束）；你自己在这个对话里说的话，它就直接问你
            if router and t is not None and t.via == VIA and not getattr(t, "sa_asked", False):
                t.sa_asked = True
                await tell(bridge, router, f"【要用户定】#{s.num} {s.name}\n\n{rep['question']}")
        return _text("已记下。" + ("问题已经转给用户，能先做的照做；没有能先做的就结束这一轮。" if rep["question"] else "现在可以结束这一轮了。"))

    @tool("handback", "这句话不属于你这件事（是另一件新事，或该交给别的对话）时调用：退回给总管重新分派。调用后直接结束这一轮，不要回答、不要去做。",
          {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]})
    async def handback(args):
        t = turn()
        if t is not None:
            t.sa_handback = str(args.get("reason") or "不属于这件事")[:500]
        return _text("已退回给总管。现在直接结束这一轮，不用回复用户。")

    return [report, handback]


# ---------- 一轮做完 ----------

def _files(turn) -> str:
    names = list(dict.fromkeys(Path(f).name for f in turn.changed_files))[:8]
    return "；改了 " + "、".join(names) if names else ""


def report_text(s: Session, turn, rep: dict | None = None) -> str:
    """派出去的对话做完一轮：给总管的回报。整段回复都带上（太长才截），选项、链接不能丢。"""
    if turn.is_error or turn.interrupted:
        head = "出错了" if turn.is_error else "被停下了"
        body = _cut("\n\n".join(x for x in (turn.error, turn.final_text) if x))
    elif rep:
        head = STATE_CN.get(rep["state"], rep["state"]) + (f"：{rep['summary']}" if rep.get("summary") else "")
        if rep.get("question"):
            head = f"在等用户定：{rep['question'].splitlines()[0][:200]}"
        body = _cut(turn.final_text)
    elif s.awaiting and s.awaiting_kind == "decide":
        head, body = f"在等用户定：{s.awaiting}", _cut(turn.final_text)
    else:
        head, body = "做完了（没按格式汇报，按回复判断）", _cut(turn.final_text)
    return f"【回报】#{s.num} {s.name}：{head}{_files(turn)}\n\n{body}"


async def on_turn_done(bridge: "Bridge", s: Session, turn) -> bool:
    """派出去的对话做完一轮。返回 True：这一轮不再直接通知用户，由总管来说。"""
    if not bridge.cfg.super_agent or s.role == ROUTER:
        return False
    router = boss_of(bridge, s)
    if not router:
        return False
    rep = getattr(turn, "sa_report", None)
    back = getattr(turn, "sa_handback", "")
    # 结构化汇报说了算：要不要用户定看 question，不再从回复文字里猜
    if rep:
        q = rep.get("question") or ""
        s.awaiting, s.awaiting_kind = (q.splitlines()[0][:200], "decide") if q else ("", "")
    s.ticket = ("error" if turn.is_error else "stopped" if turn.interrupted else "asked" if s.awaiting_kind == "decide"
                else rep["state"] if rep else "ready")
    bridge.registry.save()
    try:
        if back:   # 退回：这句话重新交给总管判断；它的这一轮不通知用户
            said = (getattr(turn, "sa_text", "") or turn.request).split(FOLLOW)[0]
            router.__dict__.setdefault("_handed", {}).setdefault(said[:80], set()).add(s.num)
            await tell(bridge, router, f"【退回】#{s.num} {s.name}：{back}\n\n这句话：{said}")
            return True
        if turn.via != VIA:   # 你直接在这个对话里说的：照旧直接告诉你；给总管记一笔
            note(router, f"#{s.num} {s.name}（用户直接说的）{STATE_CN.get(s.ticket, s.ticket)}：{(rep or {}).get('summary') or turn.final_text[:120]}")
            bridge.registry.save()
            return False
        if s.ticket == "dropped" or (getattr(turn, "sa_asked", False) and s.ticket == "asked"):
            # 不用做的、问题已经转过的：不再让总管跑一轮，记一笔
            note(router, f"#{s.num} {s.name}：{STATE_CN.get(s.ticket, s.ticket)}" + (f"（{rep['summary']}）" if rep and rep.get("summary") else ""))
            bridge.registry.save()
            return True
        await tell(bridge, router, report_text(s, turn, rep))
    except Exception:  # noqa: BLE001 - 告诉总管失败就照旧直接通知用户
        log.exception("回报给总管失败 #%s", s.num)
        return False
    return True


# ---------- 远窗重启 ----------

async def recover(bridge: "Bridge", interrupted: list[int]) -> None:
    """重启前在做的、总管派出去的活：带恢复提示重新开工。总管自己被打断了，让它看一眼现状。"""
    if not bridge.cfg.super_agent:
        return
    routers = set()
    for num in interrupted:
        s = bridge.registry.get(num)
        if not s:
            continue
        if s.role == ROUTER:
            routers.add(s.num)
        elif boss_of(bridge, s):
            log.info("重启后接着做 #%s", s.num)
            await bridge._submit(s, RECOVER, _incoming(RECOVER, VIA))
    for num in routers:
        if router := bridge.registry.get(num):
            await tell(bridge, router, "【重启】远窗重启了，打断了你上一轮。用 list_sessions 看看现状；被打断的对话已经在接着做了，"
                                       "有需要用户知道的再说，没有就一句话说明已恢复。")
