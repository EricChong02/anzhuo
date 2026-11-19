"""搜索索引：每个对话里“你说的话、它的回复”的纯文本存进本机 SQLite。
只存从记录里抽出来的文字，坏了删掉 search.db 会自动重建；搜索时直接查库，不再读几十 MB 的原始记录。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (num INTEGER PRIMARY KEY, key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lines (num INTEGER NOT NULL, ord INTEGER NOT NULL, who TEXT NOT NULL, text TEXT NOT NULL, lt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lines_num ON lines (num, ord);
"""


class SearchIndex:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 索引包含对话原文，数据库和 SQLite 辅助文件只给本机账户访问。
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        try:
            with closing(self._conn()) as c, c:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(_SCHEMA)
        except sqlite3.DatabaseError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xff not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise
            for suffix in ("", "-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            self._init()

    def keys(self) -> dict[int, str]:
        with closing(self._conn()) as c, c:
            return dict(c.execute("SELECT num, key FROM docs"))

    def key(self, num: int) -> str | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT key FROM docs WHERE num=?", (num,)).fetchone()
            return row[0] if row else None

    def put(self, num: int, key: str, items: list[dict]) -> None:
        """整段换掉这个对话的文字（一个对话的记录变了就重抽一遍）。"""
        rows = [(num, i, it["who"], it["text"], it["text"].lower()) for i, it in enumerate(items) if it.get("who") in ("you", "claude") and it.get("text")]
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM lines WHERE num=?", (num,))
            c.executemany("INSERT INTO lines VALUES (?,?,?,?,?)", rows)
            c.execute("INSERT OR REPLACE INTO docs VALUES (?,?)", (num, key))

    def drop_except(self, nums: set[int]) -> None:
        with closing(self._conn()) as c, c:
            gone = [n for (n,) in c.execute("SELECT num FROM docs") if n not in nums]
            for n in gone:
                c.execute("DELETE FROM lines WHERE num=?", (n,))
                c.execute("DELETE FROM docs WHERE num=?", (n,))

    def matches(self, q: str, keys: dict[int, str]):
        """按对话顺序、消息从新到旧返回所有命中，仅查当前账号的对话。"""
        with closing(self._conn()) as c:
            for num, key in keys.items():
                for ordinal, who, text in c.execute(
                    """SELECT ord, who, text FROM lines
                       WHERE num=(SELECT num FROM docs WHERE num=? AND key=?)
                       AND instr(lt, ?) > 0 ORDER BY ord DESC""",
                    (num, key, q.lower()),
                ):
                    yield num, ordinal, who, text
