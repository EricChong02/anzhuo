"""定期清理，免得越用越占地方：小E 每天看一次。
- 小E 交给你的截图 / 文件副本（outbox）、你从网页传上来的（inbox）：超过 60 天的删掉（原件在原处不动）。
- Claude Code 的对话记录（~/.claude/projects/*/*.jsonl）：超过 90 天没动的压缩归档到 ~/.claude/projects-archive/（gzip，能解开），
  小E 里还登记着的对话不动。"""
from __future__ import annotations

import gzip
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_FILES_DAYS = 60
ARCHIVE_AFTER_DAYS = 90
PROJECTS = Path.home() / ".claude" / "projects"
ARCHIVE = Path.home() / ".claude" / "projects-archive"


def _old(p: Path, days: int, now: float) -> bool:
    try:
        return now - p.stat().st_mtime > days * 86400
    except OSError:
        return False


def run(data_dir: Path, keep_ids: set[str], now: float | None = None,
        projects: Path = PROJECTS, archive: Path = ARCHIVE) -> dict:
    now = now or time.time()
    removed = archived = 0
    freed = 0
    for sub in ("outbox", "inbox"):
        base = data_dir / sub
        if not base.is_dir():
            continue
        for p in list(base.rglob("*")):
            if p.is_file() and _old(p, KEEP_FILES_DAYS, now):
                freed += p.stat().st_size
                p.unlink()
                removed += 1
        for d in sorted((x for x in base.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
    if projects.is_dir():
        for f in projects.glob("*/*.jsonl"):
            if f.stem in keep_ids or not _old(f, ARCHIVE_AFTER_DAYS, now):
                continue
            dest = archive / f.parent.name / (f.name + ".gz")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with f.open("rb") as src, gzip.open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            freed += f.stat().st_size - dest.stat().st_size
            f.unlink()
            archived += 1
    if removed or archived:
        log.info("定期清理：删了 %d 个旧副本，归档 %d 段旧对话，腾出约 %.0f MB", removed, archived, freed / 1e6)
    return {"removed": removed, "archived": archived, "freed": freed}
