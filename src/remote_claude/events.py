"""事件：核心在固定的几个时刻喊一声，谁关心谁订阅（接力、定时任务、总管、扩展、额度提醒……）。

- emit(name, ...)：挨个叫订阅者，按 order 从小到大；某个订阅者出错只记日志，不影响别的，也不影响核心接着走。
- claim(name, ...)：同样挨个叫，谁返回真值就停，告诉核心“这事我接了”（比如总管派的活：做完先回报总管，不直接通知你）。

现有的事件：
  turn_done(s, turn)         一轮做完、报告写好之前（接力开新对话、定时任务记结果）
  turn_report(s, turn)       claim：要不要由别人来告诉你这一轮的结果（总管）
  quota(engine, level, prev, limits, num)
                             某家额度变了档：充足 / 偏紧 / 告急 / 撞线；撞线恢复后回到 充足（见 QuotaWatch.check）
  relayed(old, new)          接力开好了新对话（总管交接派活关系、总管身份）
"""
from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger(__name__)


class Bus:
    def __init__(self) -> None:
        self._subs: dict[str, list[tuple[int, int, Callable]]] = defaultdict(list)
        self._seq = 0

    def on(self, name: str, fn: Callable, order: int = 50) -> Callable:
        """订阅；order 小的先叫，一样的按订阅先后。返回 fn，可以当装饰器用。"""
        self._seq += 1
        self._subs[name].append((order, self._seq, fn))
        self._subs[name].sort(key=lambda x: (x[0], x[1]))
        return fn

    def off(self, name: str, fn: Callable) -> None:
        self._subs[name] = [x for x in self._subs[name] if x[2] is not fn]

    async def _call(self, name: str, fn: Callable, args: tuple, kw: dict) -> Any:
        try:
            got = fn(*args, **kw)
            return await got if inspect.isawaitable(got) else got
        except Exception:  # noqa: BLE001 - 一个订阅者坏了不拖累别的
            log.exception("事件 %s 的订阅者 %s 出错", name, getattr(fn, "__qualname__", fn))
            return None

    async def emit(self, name: str, *args, **kw) -> list:
        return [await self._call(name, fn, args, kw) for _, _, fn in list(self._subs[name])]

    async def claim(self, name: str, *args, **kw) -> bool:
        for _, _, fn in list(self._subs[name]):
            if await self._call(name, fn, args, kw):
                return True
        return False
