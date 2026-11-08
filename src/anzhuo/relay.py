"""接力：长对话写交接说明、换到新对话接着做。交接提示词、“该不该提示接力”的判断、额度用完时远窗拼的交接材料，
以及接力这件事本身（Relays：能换到哪、开始接力、开新对话）。"""
from __future__ import annotations

import asyncio
import copy
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import engines, split, thread
from .config import Config
from .sessions import Job

if TYPE_CHECKING:
    from .app import Bridge

# 接力：让长对话写一份交接说明，换到新对话接着做（新对话不用背着几十万 token 的旧记录）
RELAY = """这个对话太长了，要换到一个新对话接着做。请写一份交接说明，新对话只靠它接手，所以要具体：
1. 目标：用户要做成什么、明确提过的要求和偏好；
2. 已定下的决定和理由（含否掉的做法，免得重走）；
3. 已经做完的：改了哪些文件、提交 / 推送 / 上线情况；
4. 正在做的和接下来几步；
5. 关键路径、命令、账号位置、踩过的坑；
6. 还等用户决定的问题。
长短看任务：简单的几百字就够；复杂的把约束、决定、证据和文件路径写全，宁可长一点，也别让新对话重新查找、重新试错。
只回复交接说明本身，用 Markdown；不要调用任何工具，不要再做别的事。"""

CACHE_TTL = 3300       # Claude 的提示缓存约 1 小时过期，留 5 分钟余量
COMPACT_AT = 70        # 上下文用到七成：活没做完就先压缩，不急着接力


def relay_hint(s, busy: bool, relaying: bool, now: float | None = None, cfg=None) -> dict | None:
    """该不该提示“接力到新对话”。返回 {kind: long/cold/compact, tokens, hours, pct, key}；key 用来记你点过“不用”。
    门槛见配置 relay_long（上下文太长）、relay_cold（缓存过期又不短）。
    同一件事接着做大多在读缓存，便宜；接力要另写交接、重建缓存、下一棒还得重新找材料。所以提示只问“这一段做完了吗”：
    - long：上下文到 relay_long，一段做完再接力，没做完就接着说；
    - compact：上下文用到 COMPACT_AT%，没做完先压缩、做完了再接力（窗口快满，不能再接着拖）；
    - cold：缓存过期又不短，接着说要整段重写，换件事正好接力。"""
    if relaying:
        return {"kind": "running"}
    long_at, cold_at = getattr(cfg, "relay_long", Config.relay_long), getattr(cfg, "relay_cold", Config.relay_cold)
    tk = s.context_tokens or 0
    if busy or s.relay_to or not (s.claude_id or s.codex_id) or tk < cold_at:
        return None
    idle = (now or time.time()) - s.updated
    if engines.get(s.engine).cache_hint and idle > CACHE_TTL:
        return {"kind": "cold", "tokens": tk, "hours": round(idle / 3600, 1), "key": f"cold{int(s.updated)}"}
    if (pct := s.context_pct or 0) >= COMPACT_AT:
        return {"kind": "compact", "tokens": tk, "pct": round(pct), "key": f"compact{int(pct) // 10}"}
    if tk >= long_at:
        return {"kind": "long", "tokens": tk, "key": f"long{tk // 100_000}"}
    return None


def display_items(items):
    """隐藏内部接力轮次；第一句话只展示用户原话，兼容既有记录。"""
    out, internal = [], False
    for original in items:
        it = dict(original)
        if it.get("who") == "you":
            internal = it.get("text", "").strip() == RELAY
            if internal:
                continue
            text = it.get("text", "")
            if text.startswith("（这是接力的新对话") and "用户这次说：\n\n" in text:
                it["text"] = text.split("用户这次说：\n\n", 1)[1]
        if not internal:
            out.append(it)
    return out


# ---------- 接力这件事本身：谁能接、换到哪家、写交接说明还是远窗拼材料、开新对话 ----------

HEAD = "（这是接力的新对话：之前在另一个对话里做，{why}换到这里。下面是{what}，按它接着做；不清楚的细节先看现场文件，或问用户。"
TAIL = "直接回应用户的新消息，不复述交接说明，不宣告接手；用户的选择对应接力前的问题，原有等待确认的要求仍有效。）"
WHY = {"long": "那边太长了", "switch": "用户要换 {to}，", "limit": "那边{frm}额度用完了，"}
GO = "继续"   # “换家接着做”：开好新对话替你说的第一句


def pick_of(s) -> str:
    """对话现在用的是分工表里的哪一项：Claude 的型号短名（没记型号返回空）或别家的键。"""
    if engines.get(s.engine) is engines.CLAUDE:
        return next((k for k in engines.MODELS if k in (s.model or "").lower()), "")
    return engines.get(s.engine).key


def pick_label(pick: str) -> str:
    e = engines.by_key(pick)
    return e.label if e and pick not in engines.MODELS else pick.capitalize() or "Claude"


def _cut(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…（后略）"


def _said(text: str) -> str:
    """记录里你说的话：去掉接力前言、总管转话时附的说明。"""
    if text.startswith("（这是接力的新对话") and "用户这次说：\n\n" in text:
        text = text.split("用户这次说：\n\n", 1)[1]
    return re.sub(r"\n\n（经.{1,20}?转来。.*$", "", text, flags=re.S).strip()


def _git(cwd: str) -> str:
    try:
        st = subprocess.run(["git", "-C", cwd, "status", "--short", "--branch"], capture_output=True, text=True, timeout=5)
        if st.returncode:
            return ""
        lg = subprocess.run(["git", "-C", cwd, "log", "--oneline", "-5"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = st.stdout.strip().splitlines()
    return "\n".join(lines[:30] + (["…"] if len(lines) > 30 else [])) + ("\n最近提交：\n" + lg.stdout.strip() if lg.stdout.strip() else "")


def transcript(s) -> str:
    """原对话的完整记录文件（新对话要细节时去翻）。"""
    if s.claude_id and (p := thread.transcript_path(s.claude_id)):
        return str(p)
    if s.codex_id and (hits := sorted((Path.home() / ".codex" / "sessions").glob(f"*/*/*/rollout-*{s.codex_id}.jsonl"))):
        return str(hits[-1])
    return ""


def handoff(s, records: list[dict], git: str = "", path: str = "", said_max: int = 30, turns_max: int = 6) -> str:
    """原对话没法自己写交接说明（额度用完了）时，远窗按每轮记录机械拼一份：你说过的话、每轮结论、改过的文件、
    git 现状、原记录在哪。records 是每轮记录（web 存的那份），旧的在前。"""
    said, files = [], []
    for r in records:
        if r.get("request") == RELAY:
            continue
        first = _said(r.get("said") or "") or r.get("request") or ""   # 老记录没存完整原话：用标题
        if first:
            said.append((r.get("started") or 0, first))
        for e in r.get("timeline") or []:   # 做的过程中插的话
            if e.get("kind") == "you" and (t := _said(e.get("text", ""))):
                said.append((e.get("t") or r.get("started") or 0, t))
        files += [f for f in r.get("changed_files") or [] if f not in files]
    work = [r for r in records if r.get("request") != RELAY and (r.get("final_text") or r.get("error"))][-turns_max:]
    out = [f"# 交接材料（远窗按 #{s.num} 的记录整理，不是那边写的总结）",
           f"- 原对话：#{s.num} {s.name}，工作文件夹 {s.cwd}" + (f"，另有 {'、'.join(s.add_dirs)}" if s.add_dirs else "")]
    if path:
        out.append(f"- 原对话完整记录：{path}（很长，要细节时按关键词搜着看，别整个读）")
    if s.relay_note:
        out += ["", "## 更早的交接说明（原对话是从上一个对话接力过来的）", _cut(s.relay_note, 3000)]
    if said:
        out += ["", f"## 用户说过的话（最近 {min(len(said), said_max)} 条，旧的在前）"]
        out += [f"- {time.strftime('%m-%d %H:%M', time.localtime(t)) if t else ''} {_cut(x, 400)}" for t, x in said[-said_max:]]
    if work:
        out += ["", f"## 最近 {len(work)} 轮做了什么（旧的在前）"]
        for i, r in enumerate(work):
            last = i == len(work) - 1
            out += [f"### {time.strftime('%m-%d %H:%M', time.localtime(r.get('started') or 0))} {r.get('title') or r.get('request') or ''}",
                    _cut(r.get("final_text") or f"（出错：{r.get('error')}）", 3000 if last else 800)]
            if r.get("changed_files"):
                out.append("改过：" + "、".join(Path(f).name for f in r["changed_files"][:12]))
    if files:
        out += ["", "## 改过的文件（汇总）"] + [f"- {f}" for f in files[-40:]]
    if git:
        out += ["", "## git 现状（交接时）", "```", git, "```"]
    out += ["", "最后一轮可能停在半路（额度用完时正在做）：先对照 git 现状和文件确认做到哪了，做过的别重做。"]
    return "\n".join(out)


class Relays:
    """接力：把一个对话换到新对话接着做。普通接力让原对话写交接说明；换家（Claude ↔ GPT）、换型号也走这里；
    原对话额度用完了写不了，就由远窗按记录拼交接材料（handoff）。新对话开好后发事件 relayed(旧, 新)，
    总管据此把“谁派的活”交接过去。"""

    def __init__(self, bridge: "Bridge"):
        self.b = bridge
        self.writing: dict[int, dict] = {}   # 正在写交接说明的对话 → {to, go, via}；写完开新对话
        bridge.events.on("turn_done", self._written, order=10)

    # ---------- 能换到哪 ----------
    def capped(self, s) -> bool:
        fam = split.family(pick_of(s) or "sonnet")
        return s.limit_until > time.time() or self.b.quota_levels.get(fam) == "撞线"

    def targets(self, s) -> list[dict]:
        """除了原样接力，还能换到哪（网页按钮、总管用）：[{to, label, level}]。没装、受限账号不能用、文件夹不许发、
        那家也撞线了的不列；原对话撞线时同一家的也不列（换了也接不上）。DeepSeek 不列：接复杂的活不如这两家（质量优先）。"""
        cur = pick_of(s)
        fam = split.family(cur or "sonnet")
        cands = ((["sonnet"] if cur in ("opus", "fable") else []) + ["gpt"]) if fam == engines.CLAUDE.key else ["opus", "sonnet"]
        capped, paths = self.capped(s), [s.cwd, *s.add_dirs]
        out = []
        for to in cands:
            lv = self.b.quota_levels.get(split.family(to), "充足")
            if split.usable(to, paths, bool(s.root), self.b.cfg) or lv == "撞线" or (capped and split.family(to) == fam):
                continue
            out.append({"to": to, "label": pick_label(to), "level": lv})
        return out

    # ---------- 开始接力 ----------
    def refuse(self, s, to: str = "") -> str:
        """现在不能接力的原因；能接返回空。"""
        if not s or not (s.claude_id or s.codex_id):
            return "这个对话还没开始，不用接力。"
        if s.relay_to and self.b.registry.get(s.relay_to):
            return f"已经接力到 #{s.relay_to} 了。"
        if s.num in self.writing:
            return ""
        if self.b.runner.busy(s.num):
            return "正在回答或还有排队消息，请等回答结束后再接力。"
        if to and to not in {t["to"] for t in self.targets(s)}:
            return f"现在换不了 {pick_label(to)}（没装、不能用，或那家额度也用完了）。"
        if not to and self.capped(s):
            return "额度用完了，同一家接力也接不上：换别家接着做，或等恢复。"
        if not to and not s.relay_suggested and not relay_hint(s, False, False, cfg=self.b.cfg):
            return "这个对话暂时没有接力建议。"
        return ""

    async def start(self, num: int, via: str = "web", to: str = "", go: bool = False, wait_idle: float = 0) -> str:
        """to = 换到哪（pick：sonnet / gpt …，空 = 原样）；go = 开好新对话就替你说“继续”（换家接着做、总管派的活）。
        原对话额度用完了就不让它写，远窗直接拼交接材料、马上开新对话。
        wait_idle = 最多等几秒让这一轮收完尾（一轮刚做完、报告还没发完时也算在忙）。"""
        for _ in range(int(wait_idle * 2)):
            if not self.b.runner.busy(num):
                break
            await asyncio.sleep(0.5)
        s = self.b.registry.get(num)
        if why := self.refuse(s, to):
            return why
        if num in self.writing:
            return "正在写交接说明，写完自动开新对话。"
        self._remember_question(s)
        if self.capped(s):
            new = await self.open(s, await self._handoff(s), to=to, why="limit", mechanical=True)
            if go:
                await self._go(new, via)
            return f"已接力到 #{new.num}" + (f"（{pick_label(to)}）" if to else "") + ("，接着做了。" if go else "。")
        self.writing[num] = {"to": to, "go": go, "via": via}
        ahead = self.b.runner.submit(s, Job(text=RELAY, via=via))
        return "正在写交接说明" + (f"，等手上 {ahead} 件做完再写" if ahead else "") + "，写完自动开新对话。"

    def _remember_question(self, s) -> None:
        """接力前最后一条答复（可能在等你选）：带到新对话里。"""
        question = (s.relay_pending or {}).get("text", "")
        last = self.b.runner.last.get(s.num)
        if last and last.request != RELAY:
            question = last.final_text
        if not question and self.b.web.enabled:
            question = next((r.get("final_text", "") for r in reversed(self.b.web.turn_records(s.num)) if r.get("request") != RELAY), "")
        s.relay_pending = {"awaiting": s.awaiting, "kind": s.awaiting_kind, "text": question}
        self.b.registry.save()

    async def _handoff(self, s) -> str:
        records = self.b.web.turn_records(s.num) if self.b.web.enabled else []
        git = await asyncio.to_thread(_git, s.cwd)
        return handoff(s, records, git, transcript(s))

    async def _written(self, s, turn) -> None:
        """写交接说明的那一轮做完：开新对话。写到一半额度用完（或出错）就改由远窗拼材料；你停下的就算了。"""
        plan = self.writing.pop(s.num, None)
        if plan is None or turn.interrupted:
            return
        note = (turn.final_text or "").strip()
        if turn.is_error or not note:
            new = await self.open(s, await self._handoff(s), to=plan["to"], why="limit", mechanical=True)
        else:
            new = await self.open(s, note, to=plan["to"], why="switch" if plan["to"] else "long")
        if plan["go"]:
            await self._go(new, plan["via"])

    async def _go(self, new, via: str) -> None:
        from .dingtalk import Incoming
        inc = Incoming(sender_id=self.b.user_id, sender_nick="", conversation_type="1", kind="text", text=GO, via=via)
        await self.b.submit(new, GO, inc)

    # ---------- 开新对话 ----------
    async def open(self, p, note: str, to: str = "", why: str = "long", mechanical: bool = False):
        """开接力的新对话：同文件夹、同设置；to 换家 / 换型号。交接说明随你第一句话交给它。"""
        from .sessions import Session
        now = time.time()
        engine, model, effort = p.engine, p.model, p.effort
        if to:
            alt = engines.engine_of(to)
            engine, model = alt or engines.CLAUDE.name, None if alt else engines.MODELS[to]
            if split.family(to) != split.family(pick_of(p) or "sonnet"):
                effort = None   # 两家的深度不通用，用新那家的默认
        frm = pick_label(pick_of(p)) if pick_of(p) else engines.get(p.engine).label
        head = HEAD.format(why=WHY[why].format(to=pick_label(to), frm=f"（{frm}）"),
                           what="远窗按那边的记录整理的交接材料（那边额度用完了，没法自己写）" if mechanical else "那边写的交接说明")
        s = Session(num=self.b.registry.reserve_num(), name=p.name, cwd=p.cwd, add_dirs=list(p.add_dirs),
                    owner=p.owner, root=p.root, engine=engine, model=model, effort=effort, pinned=p.pinned, ext=copy.deepcopy(p.ext),
                    task_group=p.task_group, task_group_manual=p.task_group_manual, boss=p.boss,
                    renamed=True, titled=True, worked=p.worked, seen=now, updated=now, relay_from=p.num, relay_note=note,
                    unsent=f"{head}{TAIL}\n\n{note}\n\n---\n用户这次说：")
        pending = p.relay_pending or {}
        s.relay_pending = copy.deepcopy(pending)
        s.awaiting = pending.get("awaiting", p.awaiting)
        s.awaiting_kind = pending.get("kind", p.awaiting_kind)
        if question := pending.get("text"):
            s.unsent = s.unsent.replace("\n\n---\n用户这次说：", f"\n\n接力前最后一条答复（用户可能直接选择其中的选项）：\n{question}\n\n---\n用户这次说：")
        self.b.registry.add(s)
        p.relay_to = s.num
        await self.b.events.emit("relayed", p, s)   # 总管：把“谁派的活”、总管身份交接过去
        self.b.registry.save()
        return s
