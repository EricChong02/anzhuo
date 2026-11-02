import asyncio
from datetime import datetime
import json
import time
import pytest
from anzhuo.schedules import Schedules, TZ, next_slot, parse_request

def at(day=1, hour=8, minute=0): return datetime(2026,11,day,hour,minute,tzinfo=TZ).timestamp()
def once(**kw): return dict(prompt='整理要闻',kind='once',run_at=datetime.fromtimestamp(at(2),TZ).isoformat(),**kw)
# 走 Bridge.create_schedule 的用例按真实时钟校验“必须是未来”，不能用写死的日期
def later(**kw): return dict(once(),run_at=datetime.fromtimestamp(time.time()+86400,TZ).isoformat(),**kw)

def test_once_persist_pause_missed_resume_and_no_duplicate(tmp_path):
    async def go():
        store=Schedules(tmp_path/'s.json');t=store.upsert(once(),now=at())
        key=t['id'];store.action(key,'pause')
        calls=[]
        async def dispatch(task):calls.append(task['run_id']);return 7
        await store.tick(dispatch,lambda n:False,now=at(3));assert not calls
        store.action(key,'resume')
        await store.tick(dispatch,lambda n:False,now=at(3));assert len(calls)==1 and t['target']==7
        await store.tick(dispatch,lambda n:False,now=at(3));assert len(calls)==1
        store.finish(key,t['run_id'])
        with pytest.raises(ValueError):store.action(key,'resume')
        assert not t['enabled']
        loaded=Schedules(tmp_path/'s.json');assert loaded.tasks[key]['state']=='completed'
    asyncio.run(go())

@pytest.mark.parametrize('kind,weekday',[('daily',0),('weekly',4)])
def test_recurrence_catches_up_only_once_after_long_gap(tmp_path,kind,weekday):
    async def go():
        s=Schedules(tmp_path/'s.json');t=s.upsert(dict(prompt='汇总课程',kind=kind,time='08:00',weekday=weekday),now=at())
        calls=[]
        async def dispatch(task):calls.append(task['run_id']);return 2
        await s.tick(dispatch,lambda n:False,now=at(29,12))
        assert len(calls)==1 and t['next_at']>at(29,12)
        s.finish(t['id'],t['run_id'])
        await s.tick(dispatch,lambda n:False,now=at(29,13));assert len(calls)==1
    asyncio.run(go())

def test_busy_target_waits_without_claiming_or_injecting(tmp_path):
    async def go():
        s=Schedules(tmp_path/'s.json');t=s.upsert(once(target=3),now=at())
        calls=[]
        async def dispatch(task):calls.append(task['id']);return 3
        due=t['next_at'];await s.tick(dispatch,lambda n:True,now=at(4))
        assert not calls and t['next_at']==due and t['state']=='idle'
        await s.tick(dispatch,lambda n:False,now=at(4));assert calls==[t['id']]
    asyncio.run(go())

def test_restart_during_dispatch_is_paused_not_retried(tmp_path):
    async def go():
        p=tmp_path/'s.json';s=Schedules(p);t=s.upsert(once(),now=at())
        async def dispatch(task):
            restarted=Schedules(p)
            recovered=restarted.tasks[t['id']]
            assert recovered['state']=='uncertain' and not recovered['enabled']
            return 4
        await s.tick(dispatch,lambda n:False,now=at(3))
        restarted=Schedules(p);assert restarted.tasks[t['id']]['state']=='uncertain'
    asyncio.run(go())

def test_concurrent_ticks_dispatch_once(tmp_path):
    async def go():
        s=Schedules(tmp_path/'s.json');s.upsert(once(),now=at());calls=[]
        async def dispatch(t):calls.append(t['id']);await asyncio.sleep(.01);return 1
        await asyncio.gather(*(s.tick(dispatch,lambda n:False,now=at(3)) for _ in range(3)))
        assert len(calls)==1
    asyncio.run(go())

def test_edit_active_is_rejected_and_pause_survives_finish(tmp_path):
    async def go():
        s=Schedules(tmp_path/'s.json');t=s.upsert(dict(prompt='写总结',kind='daily',time='08:00'),now=at())
        async def dispatch(t):return 1
        await s.tick(dispatch,lambda n:False,now=at(3))
        with pytest.raises(ValueError):s.upsert(dict(t,prompt='修改'))
        s.action(t['id'],'pause');s.finish(t['id'],t['run_id']);s.started(t['id'],t['run_id'])
        assert not t['enabled'] and t['state']=='completed'
        s.action(t['id'],'delete');assert not s.tasks
    asyncio.run(go())

def test_failure_pauses_and_is_not_silently_retried(tmp_path):
    async def go():
        s=Schedules(tmp_path/'s.json');t=s.upsert(once(),now=at());calls=[]
        async def dispatch(t):calls.append(1);raise ValueError('文件夹不存在')
        await s.tick(dispatch,lambda n:False,now=at(3));await s.tick(dispatch,lambda n:False,now=at(4))
        assert calls==[1] and t['state']=='failed' and t['error']=='文件夹不存在'
    asyncio.run(go())

@pytest.mark.parametrize('bad',[{'kind':'bad'},{'prompt':''},{'model':'unknown'},{'kind':'daily','time':'25:99'},{'kind':'weekly','time':'08:00','weekday':7},{'run_at':'2020-01-01T00:00'}])
def test_validation_does_not_create_partial_tasks(tmp_path,bad):
    s=Schedules(tmp_path/'s.json')
    with pytest.raises(ValueError):s.upsert(dict(once(),**bad),now=at())
    assert not s.tasks

@pytest.mark.parametrize('text,kind,time_value',[
 ('明早8点整理全球要闻','once','08:00'),('每天早上8点整理要闻','daily','08:00'),
 ('每周五下午3点汇总课程','weekly','15:00'),('两小时后接着这个对话做','once','10:00'),
 ('每天23:30生成总结','daily','23:30'),('每周日晚上八点整理','weekly','20:00')])
def test_natural_time_requests(text,kind,time_value):
    d=parse_request(text,now=at());assert d['kind']==kind
    assert d.get('time')==time_value or datetime.fromisoformat(d['run_at']).strftime('%H:%M')==time_value
    assert d['prompt']

def test_natural_invalid_time_is_not_executed_immediately():
    assert parse_request('把这句话改一下',now=at()) is None
    for text in ['每天整理全球要闻','每周五下午汇总课程','明早25点整理','0小时后整理']:
        with pytest.raises(ValueError):parse_request(text,now=at())

def test_week_boundary_uses_taipei_timezone():
    task={'kind':'weekly','weekday':0,'time':'08:00'}
    t=next_slot(task,at(1,23))
    d=datetime.fromtimestamp(t,TZ)
    assert d.weekday()==0 and d.day==2 and d.hour==8

def test_corrupt_store_is_preserved_and_other_features_can_start(tmp_path):
    path=tmp_path/'s.json';path.write_text('{bad')
    s=Schedules(path)
    assert s.load_error and path.read_text()=='{bad'
    with pytest.raises(ValueError):s.upsert(once())
    assert path.read_text()=='{bad'

def test_dispatch_error_notifies_once(tmp_path):
    async def go():
        s=Schedules(tmp_path/'s.json');s.upsert(once(),now=at());notified=[]
        async def dispatch(t):raise ValueError('路径不可用')
        async def notify(t):notified.append(t['id'])
        await s.tick(dispatch,lambda n:False,now=at(3),on_error=notify)
        await s.tick(dispatch,lambda n:False,now=at(4),on_error=notify)
        assert len(notified)==1
    asyncio.run(go())

def test_bridge_dispatch_model_metadata_and_current_conversation(tmp_path,monkeypatch):
    from types import SimpleNamespace as NS
    from anzhuo.app import Bridge
    from anzhuo.config import Config
    from anzhuo.sessions import Session
    async def go():
        b=Bridge(Config(client_id='test',allowed_user_ids=['me'],scratch_root=str(tmp_path/'scratch')),NS(secret='x'),tmp_path/'data')
        original=b.registry.add(Session(num=b.registry.reserve_num(),name='正在看的对话',cwd=str(tmp_path)))
        jobs=[];monkeypatch.setattr(b.runner,'submit',lambda s,j:jobs.append((s,j)) or 0)
        t=b.create_schedule(later(model='gpt',effort='high'));t['run_id']='run1'
        num=await b._dispatch_schedule(t)
        s,job=jobs[0]
        assert num==s.num and s.engine=='codex' and s.effort=='high'
        assert job.schedule_id==t['id'] and job.schedule_run_id=='run1' and job.via=='schedule'
        assert b.registry.current==original.num and t['target']==num
        with pytest.raises(ValueError):b.create_schedule(later(target=original.num,model='gpt'))
        # 即使队列项目已经出队等待全局名额，也必须算忙。
        original.status='queued';assert b._scheduled_busy(original.num)
    asyncio.run(go())

def test_dispatch_through_real_runner_and_mock_model(tmp_path,monkeypatch):
    from anzhuo.app import Bridge
    from anzhuo.config import Config
    class DT:
        secret='x'
        def __init__(self):self.sent=[]
        async def send_markdown(self,*args):self.sent.append(args)
    async def go():
        dt=DT();b=Bridge(Config(client_id='test',allowed_user_ids=['me'],scratch_root=str(tmp_path/'scratch')),dt,tmp_path/'data')
        async def fake_run(s,job,turn):
            await asyncio.sleep(0)
            (tmp_path/'executed.txt').write_text(job.text)
            turn.final_text='定时任务测试完成。'
        async def no_usage():pass
        monkeypatch.setattr(b.runner,'_run',fake_run)
        monkeypatch.setattr(b.runner,'refresh_usage',no_usage)
        task=b.create_schedule(later())
        await b.schedules.tick(b._dispatch_schedule,b._scheduled_busy,now=task['next_at']+1)
        await asyncio.wait_for(b.runner.workers[task['target']],2)
        assert (tmp_path/'executed.txt').read_text()==task['prompt']
        assert task['state']=='completed' and not task['enabled'] and len(dt.sent)==1
        assert b.runner.last[task['target']].schedule_id==task['id']
    asyncio.run(go())
