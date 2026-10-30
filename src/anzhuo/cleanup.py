"""定期清理，免得越用越占地方：案头每天看一次。
- 对话里的截图、文件副本（outbox / inbox）不删：它们是对话内容的一部分。
- Claude Code 的对话记录（~/.claude/projects/*/*.jsonl）：超过 90 天没动的压缩归档到 ~/.claude/projects-archive/（gzip，能解开），
  案头里还登记着的对话不动。"""
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
    for sub in ():   # 对话里的截图、文件副本不删：它们是对话内容的一部分，删了旧对话里的图就没了
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


SCRATCH_DAYS = 60
DELETED_DAYS = 30
TRASH = Path.home() / ".Trash"


def _newest(d: Path) -> float:
    """文件夹里最近一次改动的时间（文件夹本身和里面的文件，取最新的）。"""
    t = d.stat().st_mtime
    for p in d.rglob("*"):
        try:
            t = max(t, p.stat().st_mtime)
        except OSError:
            pass
    return t


def tidy_scratch(root: Path, keep: set[str], now: float | None = None, trash: Path = TRASH) -> list[str]:
    """案头新开对话时建的临时文件夹：60 天没动、也没有在用的对话指着它，就挪进废纸篓（不直接删，还能捡回来）。"""
    now = now or time.time()
    moved = []
    if not root.is_dir():
        return moved
    for d in root.iterdir():
        if not d.is_dir() or str(d) in keep or now - _newest(d) < SCRATCH_DAYS * 86400:
            continue
        dest = trash / f"案头临时-{d.name}"
        n = 1
        while dest.exists():
            n += 1
            dest = trash / f"案头临时-{d.name}-{n}"
        try:
            trash.mkdir(exist_ok=True)
            shutil.move(str(d), dest)
            moved.append(d.name)
        except OSError:
            log.warning("挪不动 %s", d, exc_info=True)
    if moved:
        log.info("临时文件夹进了废纸篓：%s", "、".join(moved))
    return moved


def purge_deleted(registry, now: float | None = None) -> list[int]:
    """删除超过 30 天的对话从名单里去掉（对话记录本身按上面 90 天归档的规矩走）。"""
    now = now or time.time()
    gone = [n for n, s in registry.sessions.items() if s.hidden and s.hidden_at and now - s.hidden_at > DELETED_DAYS * 86400]
    for n in gone:
        del registry.sessions[n]
    if gone:
        registry.save()
    return gone


CHECKUP = """【每月清理体检】请检查这台电脑上可能该清理的东西，只列清单，这一轮什么都不要删、不要挪。
范围：案头的临时文件夹、~/Downloads、~/Desktop，以及 ~/Projects 下 90 天以上没动过、超过 200MB 的文件夹。
用 du、find、ls 看大小和最后改动时间，必要时看一眼里面是什么（打开说明文件、看文件名），判断是不是临时产物、旧安装包、下载过的压缩包、重复副本。
不要碰：对话记录和案头数据（~/.claude、~/.local/share/anzhuo）、任何 git 仓库的工作目录、看起来是原始资料（照片、证件、合同、财务、他人的数据）的文件、钥匙串和配置。
最后按“要你定”的格式回复：第一行“要你定：这些要清掉吗？”，然后最多 4 个选项，例如：
A. 全部清掉（列出的 N 项，共约 X GB）
B. 只清最稳妥的几项（说明是哪几项）
C. 都先不动
推荐一项并说理由。选项前面用一张表列出每项：路径、大小、多久没动、是什么、为什么建议清。
等我回复后再动手；动手时一律挪进废纸篓（mv 到 ~/.Trash/），不要 rm，做完告诉我腾出了多少空间。"""
