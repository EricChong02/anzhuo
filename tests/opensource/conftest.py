"""开源导出闸门：私有主线本来就带着路由、做课这些，平时跑会一直红，所以默认不收集。
导出前设 ANZHUO_EXPORT_CHECK=1 再跑：pytest tests/opensource"""
import os

if not os.environ.get("ANZHUO_EXPORT_CHECK"):
    collect_ignore_glob = ["test_*.py"]
