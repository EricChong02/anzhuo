import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ResultMessage

from anzhuo import deepseek_api, thread, translate
from anzhuo.engines import CLAUDE
from anzhuo.report import Turn
from anzhuo.sessions import Job, Session


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
    assert out['updates'] == ['先查资料']
    assert all(e['k'] != 'say' for e in out['process'])


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


def test_live_reply_order_around_multiple_interjections():
    from anzhuo.web.threads import _insert_live_replies

    items = [
        {'who': 'you', 'text': '原任务', 'ts': 1},
        {'who': 'you', 'text': '第一次插话', 'ts': 3},
        {'who': 'you', 'text': '第二次插话', 'ts': 5, 'pending': True},
    ]
    timeline = [
        {'kind': 'say', 'text': '先看原问题', 't': 2},
        {'kind': 'say', 'text': '助手内部回报', 'sub': '助手', 't': 2.5},
        {'kind': 'say', 'text': '收到第一次插话', 't': 4},
        {'kind': 'say', 'text': '收到第二次插话', 'phase': 'final_answer', 't': 6},
    ]
    ordered = _insert_live_replies(items, timeline)
    assert [e['text'] for e in ordered] == [
        '原任务', '先看原问题', '第一次插话', '收到第一次插话', '第二次插话', '收到第二次插话',
    ]
    assert ordered[4]['pending']


def test_codex_uses_all_final_blocks_even_if_commentary_follows(tmp_path, monkeypatch):
    from anzhuo.engines import codex as engine
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

def test_codex_reader_ignores_child_completion_and_message():
    from anzhuo.codex import CodexConn

    async def go():
        events = []
        conn = CodexConn('/tmp', lambda method, params: events.append(method))
        conn.thread_id = 'main'
        conn.turn_id = 'current'

        def line(method, **params):
            return json.dumps({'method': method, 'params': params}).encode() + b'\n'

        async def stdout():
            yield line('item/completed', threadId='child', turnId='child-turn',
                       item={'type': 'agentMessage', 'text': '助手查完了'})
            yield line('turn/completed', threadId='child', turn={'id': 'child-turn', 'status': 'completed'})
            assert not conn.done.is_set()
            yield line('turn/completed', threadId='main', turn={'id': 'previous', 'status': 'completed'})
            assert not conn.done.is_set()
            yield line('item/completed', threadId='main', turnId='current',
                       item={'type': 'agentMessage', 'text': '主任务完成'})
            yield line('turn/completed', threadId='main', turn={'id': 'current', 'status': 'completed'})
            assert conn.done.is_set()

        conn.proc = NS(stdout=stdout())
        await conn._read()
        assert events == ['item/completed', 'turn/completed']
        assert conn.turn_status == 'completed'

    asyncio.run(go())


def test_live_progress_keeps_main_messages_after_tools_start():
    from anzhuo.report import split_updates
    updates, process = split_updates([
        {'k': 'say', 'text': '正在核对原件'},
        {'k': 'do', 'text': '打开资料'},
        {'k': 'say', 'text': '助手内部回报', 'sub': '助手'},
        {'k': 'think', 'text': '思考摘要'},
    ])
    assert updates == ['正在核对原件']
    assert [e['text'] for e in process] == ['打开资料', '助手内部回报', '思考摘要']


def test_codex_archive_keeps_interjections_in_order_without_repeating_the_final(tmp_path):
    from anzhuo.web import Web

    long_update = '正在核对资料。' * 300
    dialogue, process = Web('http://x', 0, tmp_path)._codex_dialogue([
        {'kind': 'you', 'text': '核对资料'},
        {'kind': 'say', 'phase': 'commentary', 'text': long_update},
        {'kind': 'do', 'text': '查看附件'},
        {'kind': 'you', 'text': '也看看第二份附件'},
        {'kind': 'say', 'phase': 'commentary', 'text': '第二份也已查看'},
        {'kind': 'say', 'phase': 'final_answer', 'text': '已经填完'},
        {'kind': 'say', 'text': '助手回报', 'sub': '查找助手'},
    ], '已经填完')
    assert [(e['who'], e['text']) for e in dialogue] == [
        ('you', '核对资料'), ('progress', long_update), ('you', '也看看第二份附件'),
        ('progress', '第二份也已查看'),
    ]
    assert [(entry['k'], entry['text']) for entry in process] == [('do', '查看附件'), ('say', '助手回报')]


def test_claude_archive_does_not_repeat_replies_from_before_an_interjection():
    items = [{'who': 'claude', 'text': '第一段回复', 'ts': 2},
             {'who': 'you', 'text': '中途插话', 'ts': 3},
             {'who': 'claude', 'text': '第二段回复', 'ts': 4}]
    thread.attach_records(items, [{
        'id': '1-1', 'started': 1, 'ended': 5,
        'final_text': '第一段回复\n\n---\n\n第二段回复',
    }])
    assert [it['text'] for it in items] == ['第一段回复', '中途插话', '第二段回复']
    assert items[-1]['tid'] == '1-1'
