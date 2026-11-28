"""看最近几周“额度换来了什么”（口径见 src/anzhuo/value_report.py）。
用法（在仓库根）：.venv/bin/python scripts/weekly_value.py [周数，默认 4] [--with-ai]
默认在本地统计。--with-ai 会将用户消息片段和上一轮回复结尾交给 DeepSeek 分类。"""
from __future__ import annotations

import argparse

from anzhuo import value_report
from anzhuo.config import DATA_DIR
from anzhuo.localspend import LocalSpend
from anzhuo.sessions import Registry

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="统计每周额度和任务")
    parser.add_argument("weeks", nargs="?", type=int, default=4, help="统计周数")
    parser.add_argument("--with-ai", action="store_true", help="发送对话片段给 DeepSeek 分类")
    args = parser.parse_args()
    if args.weeks < 1:
        parser.error("周数应至少为 1")
    spend = LocalSpend(DATA_DIR / "localspend.json")
    reg = Registry(DATA_DIR / "sessions.json")
    ask = value_report.deepseek_ask() if args.with_ai else None
    if args.with_ai and ask is None:
        parser.error("请先配置 DeepSeek 密钥")
    print(value_report.markdown(value_report.build(DATA_DIR, reg.sessions, spend.snaps, spend, ask, n=args.weeks)))
