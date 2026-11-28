"""后台维护：每 10 分钟读本机 Claude Code 的消耗；每天清旧副本、归档旧记录；每月 1 号派一次清理体检。
清理规则本身在 cleanup.py，消耗换算在 localspend.py；这里只管什么时候跑。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import folders
from .dingtalk import Incoming

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger(__name__)


async def spend_loop(b: Bridge) -> None:
    """每 10 分钟读本机 Claude Code 记录新写的部分（第一次要读几 GB，放后台线程）。"""
    while True:
        try:
            n = await asyncio.to_thread(b.spend.scan)
            await asyncio.to_thread(b.spend.save)
            log.debug("本机消耗：新读 %d 条", n)
        except Exception:  # noqa: BLE001
            log.warning("读本机 Claude Code 消耗失败", exc_info=True)
        await asyncio.sleep(600)


async def value_loop(b: Bridge) -> None:
    """每周“额度换来了什么”（value_report）：启动后算一次；之后每 6 小时看一眼，周额度重置、上一周结束了就重算并推送一句。"""
    from . import value_report
    path = b.data_dir / "value_report.json"
    await asyncio.sleep(120)
    while True:
        try:
            old = value_report.saved(path) or {}
            done = [w for w in value_report.weeks(b.spend.snaps) if time.time() - w[1] > 3600]
            fresh = time.time() - old.get("at", 0) < 86400
            if done and (not fresh or not any(r["start"] == done[-1][0] for r in old.get("weeks", []))):
                reps = await asyncio.to_thread(value_report.build, b.data_dir, b.registry.sessions, b.spend.snaps, b.spend)
                new_week = not any(r["start"] == done[-1][0] and time.time() - r["end"] > 3600 for r in old.get("weeks", []))
                value_report.save(path, reps)
                last = next((r for r in reversed(reps) if r["start"] == done[-1][0]), None)
                if new_week and last and old and b.web.enabled:
                    await b.web.notify("admin", "上周额度换来了什么", value_report.summary_line(last))
        except Exception:  # noqa: BLE001
            log.warning("算每周额度报告失败", exc_info=True)
        await asyncio.sleep(6 * 3600)


async def attachment_loop(b: Bridge) -> None:
    """启动后清一次收发附件，此后每小时检查。"""
    from .cleanup import purge_attachments

    while True:
        try:
            await asyncio.to_thread(purge_attachments, b.data_dir)
        except Exception:  # noqa: BLE001
            log.exception("远窗附件清理失败")
        await asyncio.sleep(3600)


async def cleanup_loop(b: Bridge) -> None:
    """每天清一次旧副本、归档 90 天前的旧对话记录（见 cleanup.py）；登记着的对话不动。"""
    from . import cleanup
    while True:
        await asyncio.sleep(600)
        try:
            keep = {s.claude_id for s in b.registry.sessions.values() if s.claude_id}
            await asyncio.to_thread(cleanup.run, b.data_dir, keep)
            using = {s.cwd for s in b.registry.sessions.values() if s.pinned or (not s.hidden and time.time() - s.updated < 60 * 86400)}
            await asyncio.to_thread(cleanup.tidy_scratch, folders._expand(b.cfg.scratch_root), using)
            await monthly_checkup(b)
        except Exception:  # noqa: BLE001
            log.exception("定期清理失败")
        await asyncio.sleep(86400 - 600)


async def monthly_checkup(b: Bridge) -> None:
    """每月 1 号起派一次 AI 体检：新开一个对话，列出建议清理的东西，最后用“要你定”问你，你点头才挪进废纸篓。"""
    from .cleanup import CHECKUP
    mark = b.data_dir / "checkup.json"
    month = time.strftime("%Y-%m")
    try:
        done = json.loads(mark.read_text()).get("month")
    except (OSError, ValueError):
        done = None
    if done is None:   # 头一次：从下个月 1 号开始
        mark.write_text(json.dumps({"month": month}))
        return
    if done == month:
        return
    mark.write_text(json.dumps({"month": month}))
    num = b.registry.reserve_num()
    inc = Incoming(sender_id=b.user_id, sender_nick="", conversation_type="1", kind="text", text=CHECKUP)
    await b.start_session(num, Path.home(), [], CHECKUP, inc, note="（每月清理体检，自动派的）", kind="simple",
                          model='gpt', engine_model='gpt-6.1-sol', effort="low")
    if s := b.registry.get(num):
        s.name, s.renamed = f"{time.strftime('%-m')} 月清理体检", True
        b.registry.save()
