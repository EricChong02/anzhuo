import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from anzhuo import helpers, helper_usage, naming, reviewer, task_groups

REAL_TITLE = naming.short_title


def test_gpt_helper_is_ephemeral_read_only_and_requires_completed_turn(monkeypatch):
    calls = []
    class Conn:
        status = 'completed'
        def __init__(self, cwd, event):
            self.event, self.cwd = event, cwd
            self.done = asyncio.Event()
            self.turn_status = self.status
        async def start(self): pass
        async def request(self, method, params):
            calls.append(params)
            assert params['ephemeral'] and params['sandbox'] == 'read-only'
            assert params['approvalPolicy'] == 'never'
            assert params['config']['features']['shell_tool'] is False
            assert params['config']['web_search'] == 'disabled'
            assert not list(Path(self.cwd).iterdir())
            return {'thread': {'id': 'helper'}}
        async def start_turn(self, text, images, effort):
            self.event('item/completed', {'item': {'type': 'agentMessage', 'id': 'a', 'text': '第一段', 'phase': 'final_answer'}})
            self.event('item/completed', {'item': {'type': 'agentMessage', 'id': 'b', 'text': '第二段', 'phase': 'final_answer'}})
            self.event('thread/tokenUsage/updated', {'tokenUsage': {'total': {'totalTokens': 12}}})
            self.done.set()
        async def close(self): pass
    monkeypatch.setattr(helpers, 'CodexConn', Conn)
    async def go():
        return await helpers.gpt_text('测试要求', '秘密测试内容', purpose='route', model='fixture')
    assert asyncio.run(go()) == '第一段\n\n第二段'
    Conn.status = 'failed'
    with pytest.raises(Exception): asyncio.run(go())
    rows = [json.loads(x) for x in helper_usage.PATH.read_text().splitlines()]
    assert [r['ok'] for r in rows] == [True, False]
    assert rows[0]['tokens']['totalTokens'] == 12
    assert '秘密' not in helper_usage.PATH.read_text()
    assert not Path(calls[0]['cwd']).exists()


def test_reviewer_backend_error_cannot_approve(monkeypatch):
    async def fail(*a, **kw): raise RuntimeError('限额')
    monkeypatch.setattr(helpers, 'gpt_text', fail)
    assert asyncio.run(reviewer.review('删除', 'rm -rf /', '/tmp', '检查文件', [])) is None


def test_naming_is_deepseek_only_and_preserves_original_on_failure(monkeypatch):
    from anzhuo import deepseek_api
    monkeypatch.setattr(deepseek_api, 'key', lambda: 'fixture')
    def fail(*a, **kw): raise RuntimeError('offline')
    monkeypatch.setattr(deepseek_api, 'chat', fail)
    assert asyncio.run(REAL_TITLE('用户说的话', '正常完整答案')) == ''
    monkeypatch.setattr(deepseek_api, 'chat', lambda *a, **kw: '远窗正文修复')
    assert asyncio.run(REAL_TITLE('用户说的话', '正常完整答案')) == '远窗正文修复'


def test_naming_does_not_send_fenced_or_blocked_folder_content(tmp_path, monkeypatch):
    from anzhuo.sessions import Runner, Registry, Session
    from anzhuo.config import Config
    async def forbidden(*a, **kw): raise AssertionError('受限资料不能用于 DeepSeek 起名')
    monkeypatch.setattr(naming, 'short_title', forbidden)
    async def done(*a): pass
    runner = Runner(Registry(tmp_path/'sessions.json'), done, cfg=Config(deepseek_block=['私密资料']))
    for session in [Session(num=1, name='受限', cwd=str(tmp_path), root=str(tmp_path)),
                    Session(num=2, name='禁区', cwd=str(tmp_path/'私密资料'))]:
        asyncio.run(runner._auto_title(session, '私密内容', '私密回答'))


def test_group_failure_keeps_existing_cache(tmp_path, monkeypatch):
    cache = tmp_path / 'cache.json'
    cache.write_text('{}')
    monkeypatch.setattr(task_groups.deepseek_api, 'key', lambda: 'fixture')
    monkeypatch.setattr(task_groups, '_deepseek', lambda *a: 'invalid')
    with pytest.raises(ValueError):
        asyncio.run(task_groups.classify([{'num': 1, 'title': '测试'}], [], cache))
    assert cache.read_text() == '{}'


def test_deepseek_header_never_shows_claude_quota():
    from anzhuo import __file__ as package
    source = (Path(package).parent / 'static/app/quota.js').read_text()
    fn = source[source.index('function renderUsage('):source.index("document.addEventListener('click', e => { if (usageOpen")]
    program = """const el={innerHTML:''}; const $=()=>el; const esc=String;
const limits=[{name:'5 小时',pct:100}],limitsGpt=[],modelLimits=x=>x;
const usageRelay={num:1,targets:[]},sel=1; let usageOpen=true;
const bindRelayAlts=()=>{},qline=x=>x.name,wan=String,relayAlt=()=>'';
""" + fn + "renderUsage({has_quota:false,mkey:'deepseek',context:3}); console.log(el.innerHTML);"
    html = subprocess.check_output([shutil.which('node'), '-e', program], text=True)
    assert '5 小时' not in html and '100%' not in html
    assert '按量计费' in html and '上下文' in html


def test_completion_notice_expires_without_click_even_after_background():
    from anzhuo import __file__ as package
    source = (Path(package).parent / 'static/app/core.js').read_text()
    fn = source[source.index('let limits = [], usageOpen'):source.index('function watchStatus()')]
    program = """const assert=require('node:assert/strict');let now=1000,timer;
Date.now=()=>now;const listeners={}; const addEventListener=(k,f)=>listeners[k]=f;
const el={className:'',hidden:true,onclick:null,classList:{remove:()=>{}}}; const $=()=>el;
const esc=String,openSession=()=>{};const clearTimeout=()=>{},setTimeout=(f,ms)=>{timer=f; assert.equal(ms,4000);return 1;};
const requestAnimationFrame=()=>{throw Error('不能在延迟的动画帧里重新显示');};
""" + fn + """
notice({num:1,name:'任务完成',status:'idle'});assert.equal(el.hidden,false);timer();assert.equal(el.hidden,true);
notice({num:2,name:'后台完成',status:'idle'});now+=5000;listeners.visibilitychange();assert.equal(el.hidden,true);
notice({num:3,name:'返回页面',status:'idle'});now+=5000;listeners.pageshow();assert.equal(el.hidden,true);
console.log('PASS');
"""
    assert 'PASS' in subprocess.check_output([shutil.which('node'), '-e', program], text=True)


def test_streamed_answer_is_full_text_outside_work_process():
    from anzhuo import __file__ as package
    source = (Path(package).parent / 'static/app/nav.js').read_text()
    fn = source[source.index('function showPartial()'):source.index('// 点手机通知进来')]
    program = """const assert=require('node:assert/strict');let attached=false;
let partialText='完整正文'.repeat(3000); const md=x=>x;
const el={innerHTML:'',className:''}; const live={insertAdjacentElement:(where,x)=>{attached=where==='beforebegin';}};
const calls=[]; const feedScroll={sync:()=>calls.push('sync'),restore:()=>calls.push('restore')};
const document={querySelector:s=>s==='.live'?live:null,createElement:()=>el};const $=()=>({});
""" + fn + "showPartial();assert(attached);assert(el.innerHTML.includes(partialText));assert.deepEqual(calls,['sync','restore']);console.log('PASS');"
    assert 'PASS' in subprocess.check_output([shutil.which('node'), '-e', program], text=True)
