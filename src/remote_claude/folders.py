"""把钉钉里说的“文件夹”变成本机路径：短名、完整路径、按名字找、临时文件夹、新建文件夹。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .config import Config

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build", ".next", "site-packages"}


def _expand(p: str) -> Path:
    return Path(p).expanduser()


def search(keyword: str, roots: list[str], skip_names: list[str], max_depth: int = 4, limit: int = 8) -> list[Path]:
    """按目录名找：完全相同排最前，其次开头相同，再次包含；同档里浅的在前。"""
    kw = keyword.lower()
    hits: list[tuple[int, int, str, Path]] = []
    for root in roots:
        base = _expand(root)
        if not base.is_dir():
            continue
        stack = [(base, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if not e.is_dir(follow_symlinks=False):
                    continue
                name = e.name
                if name.startswith(".") or name in SKIP_DIRS or any(name.startswith(s) for s in skip_names):
                    continue
                low = name.lower()
                if kw in low:
                    rank = 0 if low == kw else (1 if low.startswith(kw) else 2)
                    hits.append((rank, depth, name, Path(e.path)))
                if depth + 1 < max_depth:
                    stack.append((Path(e.path), depth + 1))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    seen: list[Path] = []
    for *_, p in hits:
        if p not in seen:
            seen.append(p)
        if len(seen) >= limit:
            break
    return seen


def candidates(token: str, cfg: Config) -> list[Path]:
    """一个词对应的候选目录：短名 > 完整路径 > 按名字找。"""
    if token in cfg.folders:
        p = _expand(cfg.folders[token])
        return [p] if p.is_dir() else []
    if token.startswith(("/", "~")):
        p = _expand(token)
        return [p] if p.is_dir() else []
    return search(token, cfg.search_roots, cfg.skip_names)


def scratch_dir(cfg: Config, num: int) -> Path:
    p = _expand(cfg.scratch_root) / f"{datetime.now():%Y%m%d}-{num}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def split_new_path(spec: str) -> tuple[str, str]:
    """“personal/菜谱整理” → ("personal", "菜谱整理")；没有斜杠时位置为空。"""
    spec = spec.strip().rstrip("/")
    if "/" not in spec:
        return "", spec
    parent, _, name = spec.rpartition("/")
    return parent, name


def display(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home):] if s.startswith(home) else s
