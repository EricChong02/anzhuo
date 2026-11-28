"""只按代理编号或任务提示摘要精确关联模型；不凭名字猜型号。"""
import hashlib
import json
import time

from .thread import transcript_path


def fingerprint(text):
    return hashlib.sha256(text.strip().encode()).hexdigest() if isinstance(text,str) and text.strip() else ''


def refresh(session, turn):
    if time.monotonic() - getattr(turn, '_agent_models_at', -10) < 5:
        return
    turn._agent_models_at = time.monotonic()
    if not session.claude_id or not turn.agents:
        return
    path = transcript_path(session.claude_id)
    if not path: return
    for child in (path.parent/session.claude_id/'subagents').glob('agent-*.jsonl'):
        try:
            if child.stat().st_mtime < turn.started-2: continue
        except OSError: continue
        agent_id=child.stem.removeprefix('agent-')
        model, digest = '', ''
        try:
            with child.open() as f:
                data=f.read(256*1024)
            for line in data.splitlines():
                try: row=json.loads(line)
                except ValueError: continue
                message=row.get('message') or {}
                content=message.get('content')
                if not digest and row.get('type')=='user':
                    text=content if isinstance(content,str) else '\n'.join(b.get('text','') for b in content or [] if isinstance(b,dict) and b.get('type')=='text')
                    digest=fingerprint(text)
                if row.get('type')=='assistant' and message.get('model'):
                    model=message['model'];break
        except OSError: continue
        if not model: continue
        exact=[a for key,a in turn.agents.items() if key.removeprefix('agent-')==agent_id]
        candidates=exact or [a for a in turn.agents.values() if digest and turn.agent_tools.get(a.get('tool'),{}).get('prompt_hash')==digest]
        if len(candidates)==1:
            candidates[0].update(model=model, model_source='observed')
