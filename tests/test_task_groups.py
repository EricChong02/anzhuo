import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from aiohttp import web
from claude_agent_sdk import AssistantMessage, TextBlock
from anzhuo import task_groups
from anzhuo.sessions import Registry, Session
from anzhuo.web import Web


def test_group_parser_rejects_missing_duplicate_foreign_and_html():
    assert task_groups.parse('{"groups":[{"name":"远窗","nums":[1,2]}]}', {1,2}) == {1:'远窗',2:'远窗'}
    for groups in ([{'name':'远窗','nums':[1]}], [{'name':'远窗','nums':[1,1]}],
                   [{'name':'远窗','nums':[1,3]}], [{'name':'\n远窗','nums':[1,2]}]):
        with pytest.raises(ValueError):
            task_groups.parse(json.dumps({'groups':groups}),{1,2})


def test_group_model_fallback_has_no_tools(monkeypatch):
    monkeypatch.setattr(task_groups.deepseek_api, 'key', lambda:'secret-not-logged')
    monkeypatch.setattr(task_groups, '_deepseek', lambda *args:'invalid json')
    async def query(*, prompt, options):
        assert options.tools == [] and options.setting_sources == [] and options.max_turns == 1
        yield AssistantMessage(content=[TextBlock(text='{"groups":[{"name":"远窗","nums":[1]}]}')],model='haiku')
    monkeypatch.setattr(task_groups,'query',query)
    result, model = asyncio.run(task_groups.classify([{'num':1,'title':'远窗界面'}],[]))
    assert result == {1:'远窗'} and model == 'Haiku'


def test_user_group_rules_follow_builtin():
    assert '第4条起' not in task_groups.system()
    text = task_groups.system('同一门课各小节归课程名\n\n练习软件统一叫考试练习软件')
    assert '4. 同一门课各小节归课程名\n5. 练习软件统一叫考试练习软件\n第4条起是用户自己定的' in text
    assert '{"groups":[{"name":"任务名"' in text


def test_group_endpoint_cache_manual_race_and_account_isolation(tmp_path, monkeypatch):
    class Request(dict):
        headers = {}
        def __init__(self, data=None, owner='admin'):
            super().__init__(owner=owner)
            self.data=data or {}
        async def json(self): return self.data
    async def go():
        reg=Registry(tmp_path/'sessions.json')
        for n in range(1,5):
            reg.add(Session(num=reg.reserve_num(),name=f'工作{n}',cwd=str(tmp_path)))
        reg.get(1).relay_to=2; reg.get(2).relay_from=1
        reg.get(3).owner='guest'
        reg.get(4).task_group='手动分组'; reg.get(4).task_group_manual=True
        w=Web('http://x',0,tmp_path); w.bridge=NS(registry=reg, cfg=NS(task_grouping=True, task_group_rules='同一门课归课程名'))
        calls=[]
        async def classify(items, existing, cache=None, rules=''):
            calls.append(items)
            assert rules == '同一门课归课程名'   # 用户自己的分组规则带给模型
            assert [i['num'] for i in items]==[2]
            assert '手动分组' in existing
            return {2:'远窗'}, 'stub'
        monkeypatch.setattr(task_groups,'classify',classify)
        r=json.loads((await w._api_task_groups(Request())).text)
        assert r['ok'] and r['count']==1
        assert reg.get(1).task_group==reg.get(2).task_group=='远窗'
        assert reg.get(3).task_group=='' and reg.get(4).task_group=='手动分组'
        await w._api_task_groups(Request())
        assert len(calls)==1
        assert Registry(tmp_path/'sessions.json').get(2).task_group=='远窗'
        with pytest.raises(web.HTTPForbidden): await w._api_task_groups(Request(owner='guest'))
        # 模型返回前用户手动移组，必须保留用户的选择。
        async def racing(items,existing,cache=None,rules=''):
            reg.get(2).task_group='新任务'; reg.get(2).task_group_manual=True
            return {2:'错误覆盖'}, 'stub'
        monkeypatch.setattr(task_groups,'classify',racing)
        await w._api_task_groups(Request({'force':True}))
        assert reg.get(2).task_group=='新任务'
    asyncio.run(go())


def test_group_cache_avoids_repeat_model_calls(tmp_path, monkeypatch):
    calls=[]
    async def model(items, existing, rules=''):
        calls.append(items)
        return {i['num']:'远窗' for i in items}, 'stub'
    monkeypatch.setattr(task_groups,'_classify',model)
    async def go():
        cache=tmp_path/'groups.json'
        items=[{'num':1,'title':'列表改动'}]
        first=await task_groups.classify(items,[],cache)
        second=await task_groups.classify(items,[],cache)
        assert first[0]==second[0]=={1:'远窗'} and len(calls)==1
        assert '列表改动' not in cache.read_text()
        await task_groups.classify([{'num':1,'title':'新标题'}],[],cache)
        assert len(calls)==2
    asyncio.run(go())


def test_course_sections_share_one_group():
    assert task_groups.normalize('数据可视化·第二节') == '数据可视化'
    assert task_groups.normalize('数据可视化·第三小节') == '数据可视化'
    assert task_groups.normalize('数据可视化·整体与素材') == '数据可视化'
    assert task_groups.normalize('数据可视化·第二节') == '数据可视化'
    assert task_groups.normalize('远窗') == '远窗'
    raw='{"groups":[{"name":"数据可视化·第二节","nums":[1]},{"name":"数据可视化·第三节","nums":[2]}]}'
    assert task_groups.parse(raw,{1,2}) == {1:'数据可视化',2:'数据可视化'}


def test_cached_course_groups_merge_without_model(tmp_path, monkeypatch):
    import hashlib
    item={'num':1,'title':'像素课'}
    sig=hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    cache=tmp_path/'cache.json'
    cache.write_text(json.dumps({'1':{'sig':sig,'group':'数据可视化·第五节'}}))
    async def forbidden(*args): raise AssertionError('旧分类合并不应调用模型')
    monkeypatch.setattr(task_groups,'_classify',forbidden)
    result,_=asyncio.run(task_groups.classify([item],[],cache))
    assert result == {1:'数据可视化'}
