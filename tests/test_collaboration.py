import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from remote_claude import collaboration as c, agent_models
from remote_claude.config import Config
from remote_claude.report import Turn
from remote_claude.sessions import Session


@pytest.fixture
def context(tmp_path,monkeypatch):
    runner=NS(registry=NS(path=tmp_path/'sessions.json'),cfg=Config())
    session=Session(1,'测试',str(tmp_path))
    turn=Turn(started=time.time())
    calls=[]
    monkeypatch.setattr(c.deepseek_api,'key',lambda:'fixture')
    def chat(system,user,key,**kw):
        calls.append(json.loads(user))
        kw['usage_sink'].update(prompt_tokens=100,completion_tokens=20,_model='observed-deepseek')
        return '{"names":["甲","乙"]}'
    monkeypatch.setattr(c.deepseek_api,'chat',chat)
    args=dict(kind='extract',task='提取姓名',data='甲、乙',acceptance='两个姓名，JSON输出')
    return runner,session,turn,args,calls


def test_simple_delivery_review_and_linked_accounting(context):
    r,s,t,a,calls=context
    result=asyncio.run(c.simple(r,s,t,a))
    assert result['state']=='completed' and len(calls)==1
    row=json.loads(c.job_path(r.registry.path.parent,'deepseek',result['job_id']).read_text())
    assert row['accepted'] is None and row['attempts'][0]['tokens']['prompt_tokens']==100
    assert t.agents['simple:'+result['job_id']]['model_source']=='observed'
    v=c.review(r.registry.path.parent,s,t,dict(backend='deepseek',job_id=result['job_id'],accepted=True,evidence='JSON解析通过，姓名逐项核对一致'))
    assert v['accepted']
    main=[dict(turn_id=row['parent_turn'],engine='claude',tokens={'total':200},cost=.1)]
    report=c.summary(r.registry.path.parent,0,main)
    assert report['accepted']==1 and report['main_turns']==1 and report['unreviewed']==0
    assert '甲' not in json.dumps(report,ensure_ascii=False)
    assert report['tasks'][0]['attempts'][0]['estimated_cny_range'] is not None
    with pytest.raises(ValueError):asyncio.run(c.simple(r,s,t,{**a,'job_id':result['job_id']}))


def test_simple_one_correction_and_cross_task_denied(context):
    r,s,t,a,_=context
    result=asyncio.run(c.simple(r,s,t,a))
    again={**a,'job_id':result['job_id']}
    with pytest.raises(ValueError):asyncio.run(c.simple(r,Session(2,'其他',s.cwd),t,again))
    with pytest.raises(ValueError):c.review(r.registry.path.parent,s,Turn(started=t.started+10),dict(backend='deepseek',job_id=result['job_id'],accepted=True,evidence='核对'))
    assert asyncio.run(c.simple(r,s,t,again))['state']=='completed'
    with pytest.raises(ValueError,match='一次'):asyncio.run(c.simple(r,s,t,again))


def test_simple_failure_unknown_usage_and_no_automatic_fallback(context,monkeypatch):
    r,s,t,a,calls=context
    def fail(*a,**kw):raise TimeoutError()
    monkeypatch.setattr(c.deepseek_api,'chat',fail)
    result=asyncio.run(c.simple(r,s,t,a))
    assert result['state']=='failed' and not calls
    report=c.summary(r.registry.path.parent,0,[])
    assert report['accepted']==0 and report['tasks'][0]['attempts'][0]['tokens'] is None


@pytest.mark.parametrize('mode',['fenced','blocked','interrupted','late'])
def test_simple_preserves_access_boundaries(context,mode):
    r,s,t,a,calls=context
    if mode=='fenced':s.root=s.cwd
    if mode=='blocked':r.cfg.deepseek_block=[Path(s.cwd).name]
    if mode=='interrupted':t.interrupted=True
    if mode=='late':t.accepting=False
    assert asyncio.run(c.simple(r,s,t,a))['state']=='skipped' and not calls


def test_model_backfill_uses_exact_id_or_unique_prompt_not_description(tmp_path,monkeypatch):
    transcript=tmp_path/'session.jsonl';transcript.touch()
    folder=tmp_path/'session'/'subagents';folder.mkdir(parents=True)
    p=folder/'agent-abc.jsonl'
    p.write_text(json.dumps({'type':'user','message':{'content':'唯一任务'}})+'\n'+json.dumps({'type':'assistant','message':{'model':'claude-opus-5-5'}})+'\n')
    monkeypatch.setattr(agent_models,'transcript_path',lambda *a:transcript)
    turn=Turn(started=1,agents={'task':{'tool':'tu','model':''}},agent_tools={'tu':{'prompt_hash':agent_models.fingerprint('唯一任务')}})
    agent_models.refresh(NS(claude_id='session'),turn)
    assert turn.agents['task']['model']=='claude-opus-5-5'
    turn=Turn(started=1,agents={'one':{'tool':'tu','model':''},'two':{'tool':'tu','model':''}},agent_tools=turn.agent_tools)
    agent_models.refresh(NS(claude_id='session'),turn)
    assert not turn.agents['one']['model'] and not turn.agents['two']['model']


def test_tool_schema_available_to_administrator_only(context):
    r,s,t,a,_=context
    assert {x.name for x in c.tools(r,s,t)}=={'delegate_simple','review_delegation'}
    s.root=s.cwd
    assert c.tools(r,s,t)==[]


def test_helpers_are_separate_and_missing_usage_not_counted_as_zero(tmp_path):
    rows=[dict(t=100,provider='codex',model='luna',purpose='route',tokens={'totalTokens':42}),
          dict(t=100,provider='codex',model='luna',purpose='route',tokens={}),
          dict(t=100,provider='deepseek',model='flash',purpose='delegate_simple',tokens={'total_tokens':99})]
    (tmp_path/'helper_usage.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    result=c.summary(tmp_path,0,[])
    assert len(result['helpers'])==1
    assert result['helpers'][0]['calls']==2 and result['helpers'][0]['token_reports']==1
    assert result['helpers'][0]['total_tokens']==42
