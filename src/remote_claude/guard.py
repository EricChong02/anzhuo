"""危险操作要你点头：对话是“全部放行”模式（远程才能顺手干活），但删东西、推代码、改数据库这类不可逆的，
先停下来在手机 / 钉钉 / 网页上问你一句，允许了才做。这里只负责认出哪些命令算危险、说成人话。"""
from __future__ import annotations

import re
import shlex

# 临时目录里删东西不打扰你
_TMP = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")

_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bgit\s+push\b"), "推送代码到远端（git push）"),
    (re.compile(r"\bgit\s+reset\s+[^;&|]*--hard\b"), "丢掉没提交的改动（git reset --hard）"),
    (re.compile(r"\bgit\s+clean\s+[^;&|]*-\w*f"), "删掉没被 Git 管的文件（git clean）"),
    (re.compile(r"\bgit\s+branch\s+[^;&|]*-D\b"), "强删分支（git branch -D）"),
    (re.compile(r"\bgit\s+stash\s+(drop|clear)\b"), "丢掉暂存的改动（git stash drop/clear）"),
    (re.compile(r"\bgit\s+(checkout\s+--\s|restore\s+(?!--staged)\S)"), "丢掉文件里没提交的改动（git checkout/restore）"),
    (re.compile(r"\b(DROP\s+TABLE|DELETE\s+FROM|TRUNCATE\s+TABLE|DROP\s+DATABASE)\b", re.I), "改数据库：删表或删数据"),
    (re.compile(r"\bsudo\s"), "用管理员权限执行（sudo）"),
    (re.compile(r"\blaunchctl\s+(bootout|unload|remove)\b"), "停掉系统后台服务（launchctl）"),
    (re.compile(r"\blaunchctl\s+kickstart\b[^;&|]*-k"), "强制重启后台服务（会打断正在干活的对话）"),
    (re.compile(r"\b(diskutil\s+(erase|partition|zero)|mkfs|dd\s+if=)"), "改磁盘（抹盘、分区）"),
    (re.compile(r"\bch(mod|own)\s+-R\b"), "批量改文件权限"),
    (re.compile(r"\bfind\b[^;&|]*\s(-delete\b|-exec\s+rm\b)"), "批量删文件（find -delete）"),
    (re.compile(r"\bssh\s[^;&|]*\b(rm\s|systemctl\s+(stop|restart|disable)|reboot|shutdown)"), "在服务器上删东西或停服务"),
]

_RM = re.compile(r"(?:^|[;&|(]\s*|\bxargs\s+|\bsudo\s+)(rm|rmdir|trash)\s+([^;&|)]*)")


def _rm_targets(cmd: str) -> list[str]:
    out = []
    for m in _RM.finditer(cmd):
        try:
            args = shlex.split(m.group(2))
        except ValueError:
            args = m.group(2).split()
        out += [a for a in args if not a.startswith("-")] or ["（没写清楚删什么）"]
    return out


def danger(cmd: str) -> str | None:
    """这条命令危险就返回一句人话（做什么），否则 None。"""
    cmd = cmd or ""
    for pat, what in _RULES:
        if pat.search(cmd):
            return what
    targets = _rm_targets(cmd)
    risky = [t for t in targets if not t.startswith(_TMP) and not t.startswith(("$TMPDIR", "${TMPDIR"))]
    if risky:
        shown = "、".join(t.rsplit("/", 1)[-1] or t for t in risky[:3]) + ("等" if len(risky) > 3 else "")
        return f"删除 {shown}"
    return None
