"""发往钉钉前，把真实学生姓名和学号换掉。名单从 CyberScope 开发库只读取出，不落盘、不写日志。"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)


class RosterGuard:
    def __init__(self, db_path: str | None, refresh_seconds: int = 3600):
        self.db_path = Path(db_path).expanduser() if db_path else None
        self.refresh_seconds = refresh_seconds
        self._pattern: re.Pattern[str] | None = None
        self._loaded_at = 0.0
        self._mtime = 0.0
        self.count = 0

    def _load(self) -> None:
        self._loaded_at = time.time()
        if not self.db_path or not self.db_path.exists():
            return
        mtime = self.db_path.stat().st_mtime
        if self._pattern is not None and mtime == self._mtime:
            return
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
            rows = con.execute("SELECT name, student_id FROM users").fetchall()
            con.close()
        except sqlite3.Error as e:
            log.warning("读学生名单失败：%s", type(e).__name__)
            return
        words = {n.strip() for n, _ in rows if n and len(n.strip()) >= 2}
        words |= {s.strip() for _, s in rows if s and len(s.strip()) >= 4}
        self.count = len(words)
        self._mtime = mtime
        self._pattern = re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))) if words else None

    def redact(self, text: str) -> tuple[str, int]:
        if time.time() - self._loaded_at > self.refresh_seconds:
            self._load()
        if not self._pattern:
            return text, 0
        hits = 0

        def sub(_m: re.Match[str]) -> str:
            nonlocal hits
            hits += 1
            return "某同学"

        return self._pattern.sub(sub, text), hits
