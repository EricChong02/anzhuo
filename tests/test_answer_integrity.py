import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ResultMessage

from remote_claude import deepseek_api, thread, translate
from remote_claude.engines import CLAUDE
from remote_claude.report import Turn
from remote_claude.sessions import Job, Session


def test_chinese_answer_with_english_source_title_is_not_translated(monkeypatch):
    monkeypatch.setattr(translate, 'enabled', True)
    text = '正文已经是中文。\n\nSources:\n- [No final report and no answers to the plane disaster](https://example.com/news)'
    assert not translate.needs_translation(text)


def test_failed_translation_never_returns_limit_or_partial_text(monkeypatch):
    monkeypatch.setattr(deepseek_api, 'key', lambda: 'fixture')
    for bad in ["You've hit your 5-hour limit · resets 3:40pm", '已翻译']:
        monkeypatch.setattr(deepseek_api, 'chat', lambda *a, **kw: bad)
        assert asyncio.run(translate.to_chinese('This paragraph explains the complete answer in detail. ' * 20)) is None


def test_translation_preserves_chinese_code_and_links(monkeypatch):
    monkeypatch.setattr(deepseek_api, 'key', lambda: 'fixture')
    def chat(system, text, key, **kw):
        assert '已有中文' not in text and 'print(' not in text and 'https://' not in text
        return '我已经核对过这次修改，所有段落都完整保留。'
    monkeypatch.setattr(deepseek_api, 'chat', chat)
    src = '已有中文。\n\nI have checked this change and preserved every paragraph completely.\n\n```python\nprint("one two three four five six seven eight")\n```\n\n[English source title with more than eight separate words here](https://example.com)'
    result, model = asyncio.run(translate.to_chinese(src))
    assert result.startswith('已有中文。') and src.split('```')[1] in result
    assert src.split('\n\n')[-1] in result and model == 'DeepSeek'


def test_history_keeps_all_final_blocks_and_messages():
    def m(kind, content): return NS(type=kind, message={'content': content}, uuid='')
    out = thread.history([m('user', '问题'), m('assistant', [
        {'type': 'text', 'text': '先查资料'}, {'type': 'tool_use', 'name': 'Read'}]),
        m('assistant', [{'type': 'text', 'text': '第一段'}, {'type': 'text', 'text': '第二段'}]),
        m('assistant', [{'type': 'text', 'text': '第三段'}])])[-1]
    assert out['text'] == '第一段\n\n第二段\n\n第三段'
    assert [e['text'] for e in out['process'] if e['k'] == 'say'] == ['先查资料']


def test_child_cannot_replace_main_answer_and_long_text_is_preserved():
    text = '完整正文' * 2000
    class Client:
        async def query(self, prompt): pass
        async def receive_messages(self):
            yield AssistantMessage(content=[TextBlock(text=text)], model='deepseek-flash')
            yield AssistantMessage(content=[TextBlock(text='助手摘要')], model='deepseek-flash', parent_tool_use_id='child')
    turn = Turn(started=1)
    last, _ = asyncio.run(CLAUDE._read(Client(), Session(num=1, name='测试', cwd='/tmp'), Job(text='测试'), turn))
    assert last == text
    assert turn.timeline[0]['text'] == text
    assert turn.timeline[-1]['sub']


def test_deepseek_rejects_truncated_completion(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'finish_reason': 'length', 'message': {'content': '不完整译文'}}]}
    monkeypatch.setattr(deepseek_api.requests, 'post', lambda *a, **kw: Response())
    with pytest.raises(ValueError):
        deepseek_api.chat('system', 'text', 'fixture', timeout=1)


def test_recorded_canonical_answer_replaces_incomplete_history():
    items = [{'who': 'claude', 'text': '最后半段'}]
    thread.attach_records(items, [{'id': '1-1', 'final_text': '第一半段\n\n最后半段', 'translated': ''}])
    assert items[0]['text'] == '第一半段\n\n最后半段'


def test_live_answer_is_separate_from_process_and_keeps_child_out():
    from remote_claude.report import live_answer
    turn = Turn(started=1, timeline=[{'kind': 'do', 'text': '查资料'},
        {'kind': 'say', 'text': '第一段'}, {'kind': 'say', 'text': '助手的话', 'sub': '助手'},
        {'kind': 'say', 'text': '第二段'}])
    answer, process = live_answer(turn)
    assert answer == '第一段\n\n第二段'
    assert [e['text'] for e in process] == ['查资料', '助手的话']
    turn.timeline.append({'kind': 'do', 'text': '接着做'})
    assert live_answer(turn)[0] == ''


def test_codex_uses_all_final_blocks_even_if_commentary_follows(tmp_path, monkeypatch):
    from remote_claude.engines import codex as engine
    class Conn:
        model, turn_status, turn_error = 'fixture', 'completed', ''
        def __init__(self, cwd, event, on_request, **kwargs):
            self.event, self.done = event, asyncio.Event()
        async def start(self): pass
        async def open_thread(self, *a): return 'fixture-thread'
        async def start_turn(self, *a):
            for i, (phase, text) in enumerate([('commentary', '先查资料'), ('final_answer', '第一段'),
                                               ('final_answer', '第二段'), ('commentary', '收尾通知')]):
                self.event('item/completed', {'item': {'type': 'agentMessage', 'id': str(i), 'phase': phase, 'text': text}})
            self.done.set()
            return 'fixture-turn'
        async def close(self): pass
    monkeypatch.setattr(engine.codex_conn, 'CodexConn', Conn)
    async def capture(*a): return ''
    monkeypatch.setattr(engine.Checkpoints, 'capture', lambda *a: '')
    turn = Turn(started=1)
    runner = NS(registry=NS(path=tmp_path/'sessions.json'), default_effort='low', language='中文',
                extra_instructions='', live={})
    session = Session(num=1, name='测试', cwd=str(tmp_path))
    asyncio.run(engine.CodexEngine().run(runner, session, Job(text='测试'), turn))
    assert turn.final_text == '第一段\n\n第二段'
