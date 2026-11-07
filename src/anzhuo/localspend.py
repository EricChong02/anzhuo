"""本机所有 Claude Code 的消耗（远窗的对话、电脑上直接开的 Claude Code、子代理都在 ~/.claude/projects 下留记录）。

管理页把“每个对话的消耗”换算成“占周额度百分之几”，要知道“周额度涨 1% 大约对应多少消耗”。
只用远窗自己的消耗当分母会把你在电脑上直接用的也算到远窗头上（偏大）；把本机全部消耗都算进分母，
剩下没算到的只有 claude.ai 网页版和 App。

只读记录里每条回复的 token 数（不读内容），按 API 标价折成美元（和远窗每轮记的金额同一把尺子，实测差约 2%），
按 5 分钟一格累加。记录有好几 GB：记住每个文件读到哪了，之后只读新写的部分。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BUCKET = 300
KEEP = 35 * 86400
# 每百万 token 的美元价（输入、输出、缓存读）；缓存写：5 分钟的 ×1.25 输入价，1 小时的 ×2。按前缀匹配，先长后短
PRICES = [("fable-5-1", 10, 50, .25), ("mythos-5-1", 10, 50, .25), ("fable", 10, 50, 1), ("mythos", 10, 50, 1),
          ("opus-5-5", 4, 20, .2), ("opus", 5, 25, .5), ("sonnet-5", 2, 10, .2), ("sonnet", 3, 15, .3),
          ("haiku-5", .1, .5, .01), ("haiku", 1, 5, .1)]


def cost(model: str, u: dict) -> float:
    m = (model or "").removeprefix("claude-")
    p = next((x for x in PRICES if m.startswith(x[0])), None)
    if not p:
        return 0.0
    _, i, o, r = p
    cc = u.get("cache_creation") or {}
    w5, w1 = (cc.get("ephemeral_5m_input_tokens", 0), cc.get("ephemeral_1h_input_tokens", 0)) if cc \
        else (u.get("cache_creation_input_tokens", 0), 0)
    c = (u.get("input_tokens", 0) * i + u.get("output_tokens", 0) * o + u.get("cache_read_input_tokens", 0) * r
         + (w5 or 0) * i * 1.25 + (w1 or 0) * i * 2) / 1e6
    return c * 2 if u.get("speed") == "fast" else c


class LocalSpend:
    def __init__(self, cache: Path, root: Path | None = None):
        self.cache, self.root = cache, root or Path.home() / ".claude" / "projects"
        self.files: dict[str, list] = {}   # 文件 → [读到第几字节, 最近几条回复的 id（一条回复会分几行写，只算一次）]
        self.buckets: dict[int, float] = {}
        self.snaps: list[list[float]] = []  # [时间, 本周额度 %]，定时读额度时记一笔
        try:
            d = json.loads(cache.read_text())
            self.files, self.snaps = d.get("files", {}), d.get("snaps", [])
            self.buckets = {int(k): v for k, v in d.get("buckets", {}).items()}
        except (OSError, ValueError):
            pass

    def scan(self, now: float | None = None) -> int:
        """读各记录新写的部分；返回这次新算进去的回复条数。很慢的第一次（几 GB）放后台线程里跑。"""
        now = now or time.time()
        n = 0
        for p in self.root.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            key = str(p)
            done, ids = self.files.get(key) or [0, []]
            if st.st_mtime < now - KEEP and key not in self.files or st.st_size == done:
                continue
            if st.st_size < done:   # 文件被改短了（重写）：从头来
                done, ids = 0, []
            try:
                with p.open("rb") as f:
                    f.seek(done)
                    data = f.read()
            except OSError:
                continue
            end = data.rfind(b"\n") + 1   # 最后半行等写完再读
            seen = set(ids)
            for ln in data[:end].splitlines():
                if b'"usage"' not in ln or b'"assistant"' not in ln:
                    continue
                try:
                    d = json.loads(ln)
                    m = d["message"]
                    t = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                except (ValueError, KeyError, TypeError):
                    continue
                mid = m.get("id") or d.get("requestId")
                if d.get("type") != "assistant" or mid in seen or t < now - KEEP:
                    continue
                seen.add(mid)
                ids.append(mid)
                c = cost(m.get("model", ""), m.get("usage") or {})
                if c:
                    b = int(t // BUCKET * BUCKET)
                    self.buckets[b] = self.buckets.get(b, 0) + c
                    n += 1
            self.files[key] = [done + end, ids[-50:]]
        self._idx = None
        self.prune(now)
        return n

    def snap(self, pct: float, now: float | None = None) -> None:
        self.snaps.append([round(now or time.time(), 1), pct])

    def prune(self, now: float) -> None:
        self.buckets = {k: v for k, v in self.buckets.items() if k >= now - KEEP}
        self.snaps = [s for s in self.snaps if s[0] >= now - KEEP]
        self.files = {k: v for k, v in self.files.items() if os.path.exists(k)}

    def save(self) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"files": self.files, "snaps": self.snaps,
                                   "buckets": {str(k): round(v, 5) for k, v in self.buckets.items()}}))
        tmp.replace(self.cache)

    def between(self, a: float, b: float) -> float:
        """a 到 b 之间本机花了多少（5 分钟一格，格子中点落在区间里就算）。"""
        import bisect
        if getattr(self, "_idx", None) is None or self._idx[0] != len(self.buckets):
            ks = sorted(self.buckets)
            acc, run = [0.0], 0.0
            for k in ks:
                run += self.buckets[k]
                acc.append(run)
            self._idx = (len(self.buckets), ks, acc)
        _, ks, acc = self._idx
        i, j = bisect.bisect_left(ks, a - BUCKET / 2), bisect.bisect_left(ks, b - BUCKET / 2)
        return acc[j] - acc[i]
