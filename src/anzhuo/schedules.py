"""持久化定时任务：先记执行凭据再提交，错过循环任务只补最近一次。"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo



def local_zone() -> ZoneInfo:
    """这台电脑现在用的时区（读 /etc/localtime 指向的时区名）；读不到就用 UTC。"""
    for name in (os.environ.get("TZ", ""), os.path.realpath("/etc/localtime").split("zoneinfo/")[-1]):
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - 名字空或不认识就试下一个
            continue
    return ZoneInfo("UTC")


TZ = local_zone()   # 配置里写了 timezone 就由 app.Bridge 启动时换成那个


def set_timezone(name: str) -> None:
    global TZ
    TZ = ZoneInfo(name) if name else local_zone()
ACTIVE = {'dispatching', 'submitted', 'running'}
MODELS = {'auto', 'opus', 'sonnet', 'haiku', 'fable', 'gpt'}

def next_slot(task: dict, now: float) -> float | None:
    if task['kind'] == 'once': return None
    d = datetime.fromtimestamp(now, TZ)
    hh, mm = map(int, task['time'].split(':'))
    slot = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if task['kind'] == 'weekly':
        slot += timedelta(days=(task['weekday'] - slot.weekday()) % 7)
    if slot.timestamp() <= now:
        slot += timedelta(days=7 if task['kind'] == 'weekly' else 1)
    return slot.timestamp()

class Schedules:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.tasks: dict[str, dict] = {}
        self.load_error = ''
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                tasks = raw['tasks']
                if not isinstance(tasks, dict) or any(not isinstance(t, dict) or not {'id','kind','enabled','state','prompt','next_at'} <= t.keys() for t in tasks.values()):
                    raise ValueError()
                self.tasks = tasks
            except (OSError, ValueError, KeyError, TypeError):
                self.load_error = '定时任务文件无法读取，原文件已保留；请修复后重启远窗'
                return
        changed = False
        for task in self.tasks.values():
            if task.get('state') in ACTIVE:
                task.update(state='uncertain', enabled=False, error='远窗在上次执行期间重启，请查看对应对话后决定是否重新安排')
                changed = True
        if changed: self.save()

    def save(self):
        if self.load_error: raise ValueError(self.load_error)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'tasks': self.tasks}, ensure_ascii=False, indent=2))
        temp.replace(self.path)

    def rows(self):
        return sorted(self.tasks.values(), key=lambda t: (not t['enabled'], t.get('next_at') or float('inf'), t['id']))

    def upsert(self, data: dict, now=None) -> dict:
        if self.load_error: raise ValueError(self.load_error)
        now = time.time() if now is None else now
        key = str(data.get('id') or '')
        old = self.tasks.get(key)
        if key and not old: raise ValueError('找不到这个定时任务')
        if old and old.get('state') in ACTIVE: raise ValueError('任务已提交，请等执行结束后再修改；可先暂停后续计划')
        prompt = str(data.get('prompt') or '').strip()
        if not prompt or len(prompt) > 20000: raise ValueError('请填写任务内容，最多 20000 字')
        kind = data.get('kind')
        if kind not in {'once', 'daily', 'weekly'}: raise ValueError('请选择单次、每天或每周')
        model = data.get('model') or 'auto'
        if model not in MODELS: raise ValueError('不支持这个模型')
        effort = data.get('effort') or 'medium'
        if effort not in {'low','medium','high','xhigh','max'}: raise ValueError('不支持这个思考深度')
        try:
            target = int(data['target']) if data.get('target') not in (None, '') else None
            if target is not None and target < 1: raise ValueError()
        except (ValueError, TypeError): raise ValueError('请选择有效的对话')
        task = dict(old or {}, id=key or uuid.uuid4().hex, title=str(data.get('title') or prompt[:24]).strip()[:80],
                    prompt=prompt, kind=kind, model=model, effort=effort, target=target,
                    folder=str(data.get('folder') or ''), enabled=True, state='idle', error='', timezone=str(TZ))
        if kind == 'once':
            try:
                d = datetime.fromisoformat(str(data.get('run_at') or ''))
                d = d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
                if d.timestamp() <= now: raise ValueError()
            except (ValueError, TypeError): raise ValueError('单次任务请选择未来的日期和时间')
            task.update(run_at=d.isoformat(), next_at=d.timestamp(), time=d.strftime('%H:%M'))
        else:
            clock = str(data.get('time') or '')
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock): raise ValueError('请填写有效时间，例如 08:00')
            task['time'] = clock
            try: weekday = int(data.get('weekday', 0))
            except (ValueError, TypeError): raise ValueError('请填写有效的星期')
            if not 0 <= weekday <= 6: raise ValueError('请填写有效的星期')
            task['weekday'] = weekday
            task['next_at'] = next_slot(task, now)
        self.tasks[task['id']] = task
        self.save()
        return task

    def action(self, key: str, action: str):
        if self.load_error: raise ValueError(self.load_error)
        task = self.tasks.get(key)
        if not task: raise ValueError('找不到这个定时任务')
        if action == 'delete': del self.tasks[key]
        elif action == 'pause': task['enabled'] = False
        elif action == 'resume':
            if task.get('state') in ACTIVE: raise ValueError('当前一轮还在执行')
            if task['kind'] == 'once' and not task.get('next_at'):
                raise ValueError('单次任务已提交过，请修改时间重新安排')
            if task['kind'] != 'once': task['next_at'] = next_slot(task, time.time())
            task.update(enabled=True, state='idle', error='')
        else: raise ValueError('不支持这个操作')
        self.save()

    def started(self, key: str, run_id: str):
        t = self.tasks.get(key)
        if t and t.get('run_id') == run_id and t.get('state') in ACTIVE:
            t['state'] = 'running'; self.save()

    def finish(self, key: str, run_id: str, error: str = ''):
        t = self.tasks.get(key)
        if t and t.get('run_id') == run_id:
            t.update(state='failed' if error else 'completed', error=error[:200], finished_at=time.time())
            self.save()

    async def tick(self, dispatch, busy, now=None, on_error=None):
        if self.load_error: return
        async with self.lock:
            now = time.time() if now is None else now
            for task in list(self.tasks.values()):
                if not task['enabled'] or not task.get('next_at') or task['next_at'] > now or task.get('state') in ACTIVE:
                    continue
                if task.get('target') and busy(task['target']): continue
                # 先保存本轮凭据与下一时刻；提交期间崩溃时标为待确认，绝不偷偷重跑。
                task.update(state='dispatching', run_id=uuid.uuid4().hex, started_at=now, error='')
                task['next_at'] = next_slot(task, now)
                if task['kind'] == 'once': task['enabled'] = False
                self.save()
                try:
                    num = await dispatch(task)
                    task['target'] = num
                    if task['state'] == 'dispatching': task['state'] = 'submitted'
                except Exception as e:
                    task.update(state='failed', enabled=False, error=str(e)[:200])
                self.save()
                if task['state'] == 'failed' and on_error:
                    try:
                        await on_error(task)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception('定时任务错误通知失败')

    async def loop(self, dispatch, busy, on_error=None):
        while True:
            try:
                await self.tick(dispatch, busy, on_error=on_error)
            except asyncio.CancelledError: raise
            except Exception:
                import logging
                logging.getLogger(__name__).exception('定时任务调度失败')
            await asyncio.sleep(5)


def parse_request(text: str, now=None) -> dict | None:
    """只识别句首的明确时间安排；缺少具体时间时要求补充，避免立即误执行。"""
    now = time.time() if now is None else now
    d = datetime.fromtimestamp(now, TZ)
    text = text.strip()
    text = re.sub(r'^定时[：:，,\s]*', '', text)
    rel = re.match(r'^(\d+|[一二两三四五六七八九十]+)\s*(分钟|小时)后[，,：:\s]*(.+)$', text, re.S)
    nums = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
    def number(raw):
        if raw.isdigit(): return int(raw)
        if raw in nums: return nums[raw]
        if '十' in raw:
            a,b=raw.split('十',1);return nums.get(a,1)*10+nums.get(b,0)
        raise ValueError('请用数字写时间')
    if rel:
        count=number(rel[1])
        if count <= 0 or count > 525600: raise ValueError('请填写有效的延后时间')
        at=d+timedelta(minutes=count*(60 if rel[2]=='小时' else 1))
        return {'kind':'once','run_at':at.isoformat(),'prompt':rel[3].strip()}
    m = re.match(r'^(每天|每周[一二三四五六日天]|今天|明天|后天|今晚|明早|明晚)(上午|下午|晚上|早上|中午|凌晨)?\s*(\d{1,2}|[一二两三四五六七八九十]+)(?:[:：](\d{2})|点(?:(半)|(\d{1,2})分?)?|时)[，,：:\s]*(.+)$',text,re.S)
    if not m:
        if re.match(r'^(?:每天|每周[一二三四五六日天]|明早|明晚|今晚)',text):
            raise ValueError('请写具体时间，例如“每天早上8点整理全球要闻”，或在定时任务中设置')
        return None
    prefix, period, raw_hour, minute, half, cnminute, prompt = m.groups()
    hour=number(raw_hour); minute=30 if half else int(minute or cnminute or 0)
    if (period in ('下午','晚上') or prefix in ('今晚','明晚')) and hour<12: hour+=12
    if period=='中午' and hour<11: hour+=12
    if not 0<=hour<=23 or not 0<=minute<=59: raise ValueError('请填写有效的时刻')
    data={'prompt':prompt.strip(),'time':f'{hour:02d}:{minute:02d}'}
    if prefix=='每天': return dict(data,kind='daily')
    if prefix.startswith('每周'):
        return dict(data,kind='weekly',weekday='一二三四五六日'.index(prefix[-1].replace('天','日')))
    days=2 if prefix=='后天' else 1 if prefix in ('明天','明早','明晚') else 0
    at=(d+timedelta(days=days)).replace(hour=hour,minute=minute,second=0,microsecond=0)
    if at.timestamp()<=now: raise ValueError('这个时刻已经过去，请换一个未来时间')
    return dict(data,kind='once',run_at=at.isoformat())
