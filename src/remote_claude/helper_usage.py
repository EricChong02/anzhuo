"""辅助调用单独记账：只存用途、模型、token 与状态，不存提示词、回复或密钥。"""
from __future__ import annotations

import json
import logging
import time

from .config import DATA_DIR

PATH = DATA_DIR / 'helper_usage.jsonl'
log = logging.getLogger(__name__)


def record(purpose: str, provider: str, model: str, started: float, *, tokens=None, error: str = '') -> None:
    row = {'t': time.time(), 'purpose': purpose, 'provider': provider, 'model': model,
           'seconds': round(time.monotonic() - started, 3), 'ok': not error,
           'tokens': tokens or {}, 'error': error}
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with PATH.open('a') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        log.warning('辅助消耗记账失败')
