"""定时任务怎么落到对话上：建任务时校验、到点交给目标对话（没有就新开一个）、做完记结果、出错时通知你。
任务的存储、时间计算和到点循环在 schedules.py；这里只管它和对话、Runner 打交道的那一半。"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import engines, folders, split
from .engines import MODELS
from .intent import _looks_like_project, inline_overrides
from .sessions import Job, Session

if TYPE_CHECKING:
    from .app import Bridge
    from .commands import Cmd
    from .dingtalk import Incoming
    from .report import Turn


class ScheduleJobs:
    def __init__(self, bridge: Bridge):
        self.b = bridge
        bridge.events.on("turn_done", self._ran, order=20)

    def create(self, data: dict) -> dict:
        b = self.b
        target = b.registry.get(int(data["target"])) if data.get("target") else None
        if data.get("target") and (not target or target.hidden):
            raise ValueError("目标对话不存在")
        model = data.get("model") or "auto"
        if target and model != "auto" and engines.switches(model, target.engine):
            raise ValueError("已有对话不能切换 Claude / Codex，请选择继承模型或新开专用对话")
        if data.get("folder") and data["folder"] not in b.cfg.folders:
            raise ValueError("请选择已配置的工作文件夹")
        return b.schedules.upsert(data)

    async def from_text(self, cmd: Cmd, inc: Incoming) -> bool:
        """话里说的是“每天 9 点帮我……”这类定时安排：建好任务并回一句，返回 True；不是就返回 False。"""
        from .schedules import TZ, parse_request
        b = self.b
        try:
            plan = parse_request(cmd.text or "")
            if not plan:
                return False
            if inc.image_codes or inc.local_images or inc.files:
                raise ValueError("定时任务请先用文字创建；需要的文件放在工作目录中")
            m, e = inline_overrides(cmd.text)
            model = "gpt" if m == "gpt" else next((k for k, v in MODELS.items() if v == m), "auto")
            plan.update(target=cmd.num or (b.registry.current if re.search("这个对话|接着|继续", cmd.text) else None),
                        model=model, effort=e or "medium")
            task = self.create(plan)
            at = datetime.fromtimestamp(task['next_at'], TZ).strftime('%m月%d日 %H:%M')
            await b.say("定时任务已安排", f"已安排：{task['title']}，下次 {at} 执行。可在网页的定时任务中修改。", board=False)
        except ValueError as ex:
            await b.say("定时任务", str(ex), board=False)
        return True

    async def _failed(self, task: dict):
        b = self.b
        body = f"{task['title']}：{task['error']}。计划已暂停，可在定时任务中修改。"
        pushed = await b.web.notify("admin", "定时任务未执行", body, task.get("target")) if b.web.enabled else 0
        if not pushed:
            await b.say("定时任务未执行", body, board=False)

    def busy(self, num: int) -> bool:
        s = self.b.registry.get(num)
        return self.b.runner.busy(num) or bool(s and s.status in ("queued", "running"))

    async def dispatch(self, task: dict) -> int:
        b = self.b
        s = b.registry.get(task.get("target"))
        if task.get("target") and (not s or s.hidden):
            raise ValueError("目标对话已收起或不存在，请修改任务")
        if s and self.busy(s.num):
            raise ValueError("目标对话正忙，请稍后重新安排")
        model = task["model"]
        if s and model != "auto" and engines.switches(model, s.engine):
            raise ValueError("目标对话的模型类型发生变化，请修改任务")
        if not s:
            num = b.registry.reserve_num()
            folder = task.get("folder")
            cwd = Path(b.cfg.folders[folder]).expanduser() if folder else folders.scratch_dir(b.cfg, num)
            if not cwd.is_dir(): raise ValueError("工作文件夹不存在")
            if await asyncio.to_thread(folders.blocked, [cwd]):
                raise ValueError("工作文件夹不可访问")
            if model == "auto":   # 没点名：按分工表（代码仓里按开发，别处按简单任务）
                c = split.choose(b.cfg, "dev" if _looks_like_project(cwd) else "simple", [cwd], levels=b.quota_levels)
                pick, full = c.pick, c.full_model
            else:
                pick, full = model, MODELS.get(model)
            alt = engines.engine_of(pick)
            s = Session(num=num, name=task['title'], cwd=str(cwd), engine=alt or engines.CLAUDE.name,
                        model=full, effort=task['effort'], renamed=True, titled=True)
            current = b.registry.current
            b.registry.add(s)
            b.registry.current = current
            b.registry.save()
            task['target'] = s.num
            b.schedules.save()
        else:
            if model != "auto" and not engines.engine_of(model): s.model = MODELS[model]
            s.effort = task['effort']
        s.awaiting = s.awaiting_kind = ""
        b.registry.save()
        job = Job(text=task['prompt'], via="schedule", schedule_id=task['id'], schedule_run_id=task['run_id'])
        b.outgoing.setdefault(s.num, []).append({'t': time.time(), 'text': task['prompt']})
        b.runner.submit(s, job)
        return s.num

    def _ran(self, s: Session, turn: Turn) -> None:
        if turn.schedule_id:
            self.b.schedules.finish(turn.schedule_id, turn.schedule_run_id,
                                    turn.error or ("任务未完成" if turn.is_error or turn.interrupted else ""))

    def loop(self):
        return self.b.schedules.loop(self.dispatch, self.busy, self._failed)
