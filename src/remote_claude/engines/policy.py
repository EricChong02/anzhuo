"""统一的工具批准：取出这一步要做的事（命令，或要删 / 挪的路径）→ guard 判危险 → 危险的问人（runner.decide）。
Claude 的 Bash 钩子、Codex 的命令批准、ACP 的批准请求、Codex 生图委派都走这里，安全规则只有这一处。"""
from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Iterable

from .. import guard

if TYPE_CHECKING:
    from ..report import Turn
    from ..sessions import Runner, Session

EXECUTE, DELETE, MOVE = "execute", "delete", "move"
KINDS = (EXECUTE, DELETE, MOVE)   # 要判的几类；别的（读、改文件、上网……）不拦
_UNSEEN = {EXECUTE: "要跑命令，但看不出具体是什么", DELETE: "要删文件，但看不出删什么", MOVE: "要挪文件，但看不出挪什么"}


def _paths(target: str | Iterable[str] | None) -> list[str]:
    items = [target] if isinstance(target, str) else list(target or [])
    return [str(p) for p in items if p]


def danger(kind: str, target: str | Iterable[str] | None, strict: bool = False) -> str | None:
    """这一步危险就返回一句人话（做什么），否则 None。
    execute：target 是命令；strict 时看不出命令也算危险（ACP 的批准请求常常不带参数）。
    delete / move：target 是路径，按和 `rm <路径>` 一样的规则判（临时目录里的放行）；看不出路径就问，挪文件要看到源和目标。"""
    if kind == EXECUTE:
        cmd = str(target or "")
        if not cmd.strip():
            return _UNSEEN[kind] if strict else None
        return guard.danger(cmd)
    if kind in (DELETE, MOVE):
        paths = _paths(target)
        if not paths or (kind == MOVE and len(paths) < 2):   # 挪文件只看到一个路径：看不出从哪挪到哪，问你
            return _UNSEEN[kind]
        what = guard.danger("rm -- " + " ".join(shlex.quote(p) for p in paths))
        return "挪走 " + what.removeprefix("删除 ") if what and kind == MOVE else what
    return None


def shown(kind: str, target: str | Iterable[str] | None) -> str:
    """给你（或审核员）看的那一步：命令原文，或“删除：路径”。"""
    if kind == EXECUTE:
        return str(target or "")
    paths = _paths(target)
    return ("删除：" if kind == DELETE else "移动：") + " ".join(paths) if paths else ""


async def approve(runner: "Runner", s: "Session", turn: "Turn", kind: str, target: str | Iterable[str] | None,
                  *, strict: bool = False, title: str = "") -> bool | None:
    """要做这一步：不危险返回 None（照常放行，不打扰你）；危险的交给 runner.decide（审核员或问你），返回同不同意。
    title：看不出具体内容时给你看的说明（工具标题之类）。"""
    what = danger(kind, target, strict)
    if not what:
        return None
    return await runner.decide(s, turn, what, shown(kind, target) or title)
