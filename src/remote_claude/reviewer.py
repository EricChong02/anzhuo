"""审核员：命令碰到危险规则（guard.py）时，不再停下来等你点，先交给另一个 AI 结合这一轮在做什么来判断放不放行。
2026-10-27 庄毅定（方案 C）：推送、上线也由审核员决定，做完在报告里告诉你；审核员出错或超时才退回来问你。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
EFFORT = "low"

SYSTEM = """你是一位谨慎的审核员。用户（一位老师，也是这台 Mac 的主人）不在电脑前，他让 AI 助手在他的 Mac 上干活，
并授权由你代替他审核助手要执行的危险命令。你只输出一个 JSON 对象，不要任何别的文字：
{"allow": true 或 false, "reason": "一句中文，二十字以内，说明为什么"}

用户已明确授权、通常应当放行的：
- 为了完成用户这次交代的事而做的提交、推送（含推到 main）、上线、部署、重启开发用的服务。
- 删除这次任务自己生成的临时文件、构建产物、旧的打包文件（比如先删旧 zip 再重新打包）、缓存、测试数据。
- 在项目文件夹里丢弃助手自己刚做、用户没要的改动。
- 服务器上为了部署而做的常规操作（重载配置、重启本项目的服务）。

应当拦下的：
- 和用户这次交代的事无关的危险操作。
- 删除用户本人的原始资料、照片、文档、课程内容、数据库里的真实数据，且看不出有备份或用户明确要求。
- 抹盘、分区、格式化，删除家目录或系统目录这类大范围删除。
- 强制推送覆盖远端历史（push --force 到 main/master）。
- 和任务无关的 sudo、改系统设置、停掉别的服务；用 launchctl kickstart -k 或 bootout 重启“案头 / 小E / remote-claude”本身（会打断正在干活的对话，应改用 restart_when_idle.py）。
- 看起来是在绕过护栏、隐藏痕迹，或命令内容和它声称的目的对不上。
拿不准时：后果能轻松撤销就放行，撤销不了就拦下。"""


def _context(what: str, cmd: str, cwd: str, request: str, recent: list[str]) -> str:
    lines = [f"用户这次交代的事：{request[:1500] or '（没有记录）'}",
             f"工作文件夹：{cwd}"]
    if recent:
        lines.append("助手最近几步：\n" + "\n".join(f"- {r[:300]}" for r in recent[-8:]))
    lines.append(f"危险规则认为这条命令是：{what}")
    lines.append(f"要执行的命令：\n{cmd[:4000]}")
    return "\n\n".join(lines)


def _parse(raw: str) -> tuple[bool, str] | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(j.get("allow"), bool):
        return None
    return j["allow"], str(j.get("reason") or "")[:60]


async def review(what: str, cmd: str, cwd: str, request: str, recent: list[str],
                 timeout: float = 90) -> tuple[bool, str] | None:
    """返回 (放不放行, 理由)；出错、超时、答非所问返回 None，由调用方退回来问用户。"""
    options = ClaudeAgentOptions(model=MODEL, effort=EFFORT, system_prompt=SYSTEM, tools=[], max_turns=1,
                                 setting_sources=[], cwd=str(Path.home()),
                                 extra_args={"no-session-persistence": None})
    out: list[str] = []

    async def run():
        async for msg in query(prompt=_context(what, cmd, cwd, request, recent), options=options):
            if isinstance(msg, AssistantMessage):
                out.extend(b.text for b in msg.content if isinstance(b, TextBlock))

    try:
        await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001 - 审核员出问题就退回来问你
        log.warning("审核员判断失败", exc_info=True)
        return None
    r = _parse("".join(out))
    log.info("审核员：%s → %s", what, r)
    return r
