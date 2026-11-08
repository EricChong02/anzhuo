"""扩展：私有功能（比如做课）放在 extensions/名字/ 里，核心只留挂接点；导出开源版时整个目录不带，核心照样能跑。

一个扩展是 extensions/名字/__init__.py 里的 setup(api, conf) -> Extension | None：
  conf 是配置里 [ext.名字] 那一节（没写这一节就不加载；写了 enabled = false 也不加载）。
扩展只通过 Api 碰核心（发话、新开对话、读每轮记录、存自己的数据……），不直接调 Bridge / Web 的私有方法，
核心重构时只要 Api 不变，扩展就不用跟着改。

挂接点（Extension 上的方法，都可以不写）：
  routes(r)                 加地址：r.get / r.post 挂在 /_x/ext/名字/ 下，只给管理员；r.fallback 接住其余所有地址（排在最后）
  turn_links(files, turn)   一轮改到了这些文件 → 回复里附哪些链接 [{label, url, title, icon, key}]（存进这一轮的记录）
  upgrade_record(rec)       读每轮记录时把老样子的记录改成现在的样子（不写回文件）
  sessions_extra(rows)      /api/sessions 里附带的数据（放在 ext.名字；受限账号不给）
  thread_extra(s)           /api/thread 里附带的数据（放在 ext.名字）
  prepare_new(data, text)   网页新开对话前：返回 {folder, text, kind} 就照这个开（比如“接着做某门课”）
  history_top()             历史记录页顶上加的一块 HTML
  scripts                   主页面要加载的前端脚本（扩展 static/ 里的文件名）；脚本用 window.XE 挂到界面上
  close()                   退出时收尾
事件（turn_done、quota……，见 events.py）用 api.on 订阅。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from aiohttp import web

    from .app import Bridge
    from .sessions import Session

log = logging.getLogger(__name__)
PKG = __package__ + ".extensions"
STATIC_FILE = re.compile(r"[\w.-]+(/[\w.-]+)*")


class Extension:
    name = ""
    scripts: tuple[str, ...] = ()

    def __init__(self, api: "Api", conf: dict):
        self.api, self.conf = api, conf

    @property
    def static_dir(self) -> Path:
        return Path(importlib.import_module(type(self).__module__).__file__).parent / "static"

    def routes(self, r: "Routes") -> None: ...
    def turn_links(self, files: list[str], turn) -> list[dict]: return []
    def upgrade_record(self, rec: dict) -> None: ...
    def sessions_extra(self, rows: list[dict]) -> Any: return None
    def thread_extra(self, s: "Session") -> Any: return None
    async def prepare_new(self, data: dict, text: str) -> dict | None: return None
    def history_top(self) -> str: return ""
    async def close(self) -> None: ...


class Routes:
    """给一个扩展加地址。get / post 的 path 写扩展里的相对地址（"/api/feedback"），实际挂在 /_x/ext/名字/api/feedback。"""

    def __init__(self, app: "web.Application", px: str, name: str, allow_fallback: bool):
        self.app, self.base, self.allow_fallback = app, f"{px}/ext/{name}", allow_fallback
        self.fallbacks: list[Callable] = []

    def url(self, path: str) -> str:
        return self.base + path

    def get(self, path: str, handler: Callable) -> None:
        self.app.router.add_get(self.url(path), handler)

    def post(self, path: str, handler: Callable) -> None:
        self.app.router.add_post(self.url(path), handler)

    def fallback(self, handler: Callable) -> None:
        """没登记的地址都交给它（核心最后才注册）。网页挂在子路径下时不接，那时其余地址本来就不归远窗。"""
        if self.allow_fallback:
            self.fallbacks.append(handler)


class Api:
    """扩展能用的核心功能。只加不改：改了签名，扩展就会坏。"""

    def __init__(self, bridge: "Bridge", name: str):
        self._b, self.name = bridge, name

    # ---- 配置、地址 ----
    @property
    def cfg(self):
        return self._b.cfg

    @property
    def px(self) -> str:
        """远窗自己页面的前缀（/_x，挂子路径时带上前缀）。"""
        return self._b.web.px

    def url(self, path: str = "") -> str:
        """这个扩展自己的地址：/_x/ext/名字 + path。"""
        return f"{self.px}/ext/{self.name}{path}"

    def link(self, path: str) -> str:
        """给手机通知、钉钉用的完整外网链接。"""
        return self._b.web.link(path)

    # ---- 对话 ----
    def sessions(self, owner: str = "admin") -> list["Session"]:
        return [s for s in self._b.registry.visible() if s.owner == owner]

    def session(self, num: int) -> "Session | None":
        return self._b.registry.get(num)

    def data(self, s: "Session") -> dict:
        """存在对话上的扩展数据（跟着 sessions.json 存；改完调 save()）。"""
        return s.ext.setdefault(self.name, {})

    def save(self) -> None:
        self._b.registry.save()

    def stored(self) -> dict:
        """sessions.json 里原样存着的对话（编号 → 字段，含核心已经不认的老字段）：扩展迁移自己的老数据用。"""
        import json
        try:
            return json.loads(self._b.registry.path.read_text()).get("sessions", {})
        except (OSError, ValueError, AttributeError):
            return {}

    def records(self, num: int) -> list[dict]:
        """这个对话每一轮的记录（新的在后）：id、title、started、ended、links、final_text……"""
        return self._b.web._records(num)

    def status(self, num: int) -> dict:
        """这个对话现在怎么样：state = running / awaiting / idle，step = 在做哪一步，minutes = 做了几分钟。"""
        b = self._b
        s, turn = b.registry.get(num), b.runner.turns.get(num)
        if not s:
            return {}
        state = "running" if turn or b.runner.busy(num) else ("awaiting" if s.awaiting and s.awaiting_kind != "suggest" else "idle")
        step = ((turn.plan and next((x["text"] for x in turn.plan if x["status"] == "doing"), "")) or (turn.steps[-1] if turn.steps else "")) if turn else ""
        return {"num": num, "name": s.name, "state": state, "step": step, "updated": s.updated,
                "minutes": int((time.time() - turn.started) // 60) if turn else 0}

    def record(self, who: str, text: str, **extra) -> None:
        """记一行到最近来往（排查用）。"""
        self._b.record(who, text, **extra)

    def _incoming(self, text: str, images: list[str], via: str):
        from .dingtalk import Incoming
        return Incoming(sender_id=self._b.user_id, sender_nick="", conversation_type="1", kind="text", text=text,
                        local_images=list(images), via=via or self.name)

    async def send(self, num: int, text: str, images: list[str] = (), via: str = "") -> None:
        """替你在 #num 里说一句（正在做就插话，没在做就开一轮）。"""
        from .commands import Cmd
        await self._b.message(Cmd("message", num=num, text=text), self._incoming(text, images, via), routed=True)

    async def new(self, folder: str, text: str, images: list[str] = (), kind: str | None = None, note: str = "",
                  via: str = "") -> "Session | None":
        """在 folder 里新开一个对话，开头就说 text；kind = design / dev / simple（照分工表选模型）。返回新对话。"""
        before = self._b.registry.next_num
        await self._b.new_session([folder] if folder else [], text, self._incoming(text, images, via), note=note, kind=kind)
        return self._b.registry.get(before)

    def on(self, event: str, fn: Callable, order: int = 50) -> None:
        """订阅核心事件（见 events.py）。"""
        self._b.events.on(event, fn, order)

    # ---- 网页 ----
    def page(self, title: str, body: str, status: int = 200) -> "web.Response":
        return self._b.web._page(title, body, status)

    def same_origin(self, request: "web.Request") -> bool:
        return self._b.web._same_origin(request)

    async def save_images(self, images: list[str]) -> list[str]:
        """网页传来的图片（data URL）存成本机文件，返回路径。"""
        return await self._b.web._save_images({"images": list(images)})


def load(bridge: "Bridge") -> list[Extension]:
    """按配置里的 [ext.名字] 加载 extensions/ 下的扩展；目录不在（开源版）就什么都没有。"""
    try:
        pkg = importlib.import_module(PKG)
    except ImportError:
        return []
    out = []
    for info in pkgutil.iter_modules(pkg.__path__):
        conf = (bridge.cfg.ext or {}).get(info.name)
        if conf is None or conf.get("enabled", True) is False:
            continue
        try:
            mod = importlib.import_module(f"{PKG}.{info.name}")
            e = mod.setup(Api(bridge, info.name), conf)
        except Exception:  # noqa: BLE001 - 扩展坏了不拖累核心
            log.exception("扩展 %s 加载失败", info.name)
            continue
        if e is not None:
            e.name = info.name
            out.append(e)
            log.info("扩展 %s 已加载", info.name)
    return out
