"""引擎接口（Strategy）：一轮怎么跑、中途怎么插话、怎么停，各家自己实现；能力差异集中写在类属性上，别处只查这里。
另有几家共用的小工具：文件夹探测、“交给你：”的约定与解析。"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable

from ..folders import blocked, display

if TYPE_CHECKING:
    from ..report import Turn
    from ..sessions import Job, Runner, Session

COMPACT = "/compact"  # Claude Code 的压缩上下文指令，作为一句话发给对话即可（Codex 换成它自己的压缩）
CONFIRM_WAIT = 20 * 60  # 等你确认危险操作最多多久，过了当你不同意


class FolderBlocked(Exception):
    """要用的文件夹打不开：多半是 macOS 隐私保护在等你点“允许”。"""


async def check_folders(s: "Session") -> None:
    if stuck := await asyncio.to_thread(blocked, [Path(s.cwd), *map(Path, s.add_dirs)]):
        raise FolderBlocked("、".join(display(p) for p in stuck))


def role(runner: "Runner", s: "Session") -> dict:
    """特殊角色（总管和它派出去的对话）的额外工具和约定：{"tools", "append", "disallowed", "on_stop", …}；普通对话是空的。"""
    fn = getattr(runner, "role", None)
    return (fn(s) if fn else None) or {}


class Engine:
    name = ""          # 登记名（Session.engine）
    label = ""         # 显示名：Claude / GPT / Kimi
    tool = ""          # 干活的工具（GPT 是 Codex 在干）；空 = 就是它自己
    key = ""           # 选它用的键（网页模型菜单、话里“用 gpt”、总管的 engine 参数）
    pick_model = ""    # 型号怎么选：menu = 远窗的型号表（opus…，存全名）；own = 它自己报的型号；空 = 不选，用它自己配置的
    history = "records"   # 对话记录在哪：transcript = 引擎自己的会话记录（~/.claude）；records = 远窗的每轮记录
    can_steer = False   # 干活中途能插话
    can_rewind = False  # 能“回到这里”
    can_fork = False    # 能分叉
    has_quota = False   # 能查额度
    has_tools = False   # 能带远窗给的工具（交图、总管派活的汇报）
    stop_hook = False   # 支持角色的 on_stop（该汇报没汇报就让它补上再结束）
    fenced_ok = False   # 受限账号能用（要有护栏和沙箱）
    cache_hint = False  # 隔久了再聊要提示“缓存过期”
    cum_usage = False   # 报的消耗是会话累计的金额（要和上一轮相减）

    @property
    def full(self) -> str:
        return f"{self.label}（{self.tool}）" if self.tool else self.label

    @property
    def brand(self) -> str:
        return f"{self.label} · {self.tool}" if self.tool else self.label

    def available(self) -> bool:
        return True

    def refuses(self, cfg, paths: list) -> str:
        """这些文件夹能不能交给它（按配置）；不能就返回拦下它的那个文件夹名。"""
        return ""

    def models(self) -> tuple[list[str], str]:
        """pick_model = own 时能选的型号和不选时用哪个（给你、总管看的说明）。"""
        return [], f"{self.tool or self.label} 自己配置的模型"

    async def run(self, runner: "Runner", s: "Session", job: "Job", turn: "Turn") -> None:
        """跑一轮：连接放进 runner.live[s.num]（插话、停下用），结束时自己拿掉；结果写进 turn。"""
        raise NotImplementedError

    def steer(self, runner: "Runner", s: "Session", turn: "Turn", job: "Job") -> Awaitable[None] | None:
        """干活中途插话：能插就返回要在后台跑的协程（插不进去由它自己排到下一轮）；不能插返回 None，排队。"""
        return None

    async def halt(self, conn: Any) -> None:
        """停下正在跑的这一轮（conn 是 run 放进 runner.live 的连接）。"""

    def limits(self, runner: "Runner") -> dict:
        return {}

    async def refresh_quota(self, runner: "Runner", force: bool = False) -> None:
        pass

    def unfinished(self, runner: "Runner", s: "Session", turn: "Turn", again: bool) -> str:
        """这一轮要结束时再看一眼（角色的 on_stop）：返回一句话 = 还差什么，让它补上再结束；空 = 可以结束。
        again：已经拦过一次了。Claude 用 Stop 钩子问，Codex 做完后问、要补就再开一轮。"""
        check = role(runner, s).get("on_stop") if self.stop_hook else None
        return check(turn, again) if check else ""


# ---------- 没有远窗工具的引擎（GPT、ACP）共用：约定写在说明里，交图交文件靠最后一行“交给你：” ----------

CODEX_RULES = ("需要用户在几个方案里选时，第一行写“要你定：”加一句问题，选项写成“A. …”，推荐写成“推荐：A，理由”。"
               "要把截图或文件（PDF、表格等）交给用户看，在回复最后单独一行写“交给你：”加文件的绝对路径（多个用顿号隔开）；"
               "用户在手机上看，点不开本机路径，不要只写成 Markdown 下载链接。")


def codex_instructions(language: str = "简体中文", extra: str = "") -> str:
    lang = f"回复一律用{language}；专有名词、代码、命令、路径保持原样。" if language else ""
    return (lang + CODEX_RULES + (" " + extra.strip() if extra.strip() else "")).strip()


IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# 回复里写成下载链接的本机文件，这几类当作要交给你的（源码、说明文档之类的链接只是出处，不发）
GIVE_EXT = IMG_EXT + (".zip", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".mp4", ".mov", ".mp3", ".7z", ".tar", ".gz")
LINK = re.compile(r"\[([^\]]+)\]\((/[^)\s]+|~/[^)\s]+|[A-Za-z]:[\\/][^)\s]+)\)")


def handoff(text: str, cwd: str) -> tuple[str, list[str], list[str]]:
    """GPT 回复里要交给你的截图和文件：最后一行“交给你：路径”；它常常只写成 [下载…](/本机/路径) 链接，
    这种链接你在手机上点不开，指向能交的文件就拿出来照常发（网页、钉钉），链接只留文字。"""
    imgs: list[str] = []
    files: list[str] = []

    def take(raw: str) -> bool:
        fp = Path(raw.strip("`'\"")).expanduser()
        if not fp.is_absolute():
            fp = Path(cwd) / fp
        if not fp.is_file() or str(fp) in imgs + files:
            return fp.is_file()
        (imgs if fp.suffix.lower() in IMG_EXT else files).append(str(fp))
        return True

    keep = []
    for line in text.splitlines():
        m = re.match(r"^\s*交给你[:：]\s*(.+)$", line)
        if m:
            for raw in re.split(r"[,，、;；\s]+", m.group(1)):
                if raw:
                    take(raw)
            continue
        keep.append(LINK.sub(lambda m: f"**{m.group(1)}**" if Path(m.group(2)).suffix.lower() in GIVE_EXT and take(m.group(2))
                             else m.group(0), line))
    return "\n".join(keep).strip(), imgs, files
