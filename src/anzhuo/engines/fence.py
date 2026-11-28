"""受限账号（role = user）对话的边界：能读哪、能写哪、临时文件放哪、命令能用哪些本机工具。

- 写：只有自己的文件夹（root）和自己的临时目录。
- 读：再加上账号配置里的 read_dirs（比如课标目录，只读）和本机工具链（TOOL_DIRS，只读）。
- 临时目录：每个账号单独一个（/private/tmp/remote-window/<账号>），不和 admin 共用 /tmp/claude-<uid>，
  那里有 admin 对话的截图和中间文件，受限账号读不到。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TMP_BASE = Path("/private/tmp/remote-window")
# 本机工具链：node/npx、Python、pdftotext/ffmpeg、pandoc、做课用的 Python 库（remote-window-tools，scripts/setup_fenced_tools.sh 装）
TOOL_DIRS = ("~/.local/bin", "~/.local/share/node", "~/.local/share/mamba/envs/media", "~/.local/share/uv/python",
             "~/.local/opt", "~/.local/lib/node_modules", "~/.local/share/remote-window-tools")
TOOLS_VENV = "~/.local/share/remote-window-tools/venv"


def _abs(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()


def private_tmp(owner: str) -> Path:
    """这个账号自己的临时目录（Claude Code 会在里面再建 claude-<uid>）。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", owner or "user") or "user"
    return TMP_BASE / safe


def ensure_tmp(owner: str) -> Path:
    TMP_BASE.mkdir(parents=True, exist_ok=True)
    p = private_tmp(owner)
    p.mkdir(exist_ok=True)
    os.chmod(p, 0o700)
    return p


def tool_dirs() -> list[Path]:
    return [p for p in (_abs(d) for d in TOOL_DIRS) if p.is_dir()]


def read_dirs(account: dict | None) -> list[Path]:
    """账号配置里额外给的只读目录（不存在的不算）。"""
    raw = (account or {}).get("read_dirs") or []
    return [p for p in (_abs(d) for d in raw if isinstance(d, str) and d.strip()) if p.is_dir()]


def inside(p: Path, dirs) -> bool:
    return any(p == d or p.is_relative_to(d) for d in dirs)


def siblings_to_deny(allowed: list[Path], home: Path) -> list[Path]:
    """家目录里，除了 allowed 这几处（和通往它们的上层文件夹本身）之外的东西都列出来，拿去拒绝。
    比如只放行 ~/a/b 和 ~/c/d：~ 下除 a、c 外全拒；~/a 下除 b 外全拒；~/c 下除 d 外全拒。"""
    allowed = [a for a in allowed if a.is_relative_to(home)]
    chain: set[Path] = set()
    for a in allowed:
        cur = a
        while cur != home:
            cur = cur.parent
            chain.add(cur)
    out: list[Path] = []
    for d in sorted(chain):
        if inside(d, allowed):   # 已经在放行范围里了，不用再往下拆
            continue
        try:
            kids = list(d.iterdir())
        except OSError:
            continue
        for k in kids:
            if k in chain or k in allowed:
                continue
            out.append(k)
    return out


def path_env(base: str | None = None) -> str:
    venv = _abs(TOOLS_VENV) / "bin"
    parts = [str(venv)] if venv.is_dir() else []
    parts += [str(_abs("~/.local/bin")), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(parts)
