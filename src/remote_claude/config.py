"""配置与密钥：配置文件在 ~/.config/remote-claude/config.toml，钉钉应用密钥只存在 macOS 钥匙串。"""
from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("REMOTE_CLAUDE_CONFIG", HOME / ".config/remote-claude/config.toml"))
DATA_DIR = Path(os.environ.get("REMOTE_CLAUDE_DATA", HOME / ".local/share/remote-claude"))
KEYCHAIN_SERVICE = "remote-claude.dingtalk"

DEFAULT_SEARCH_ROOTS = ["~/Projects", "~/Desktop", "~/Documents", "~/Downloads"]
DEFAULT_SKIP_NAMES = ["_原始私密"]


@dataclass
class Config:
    client_id: str
    allowed_user_ids: list[str]
    folders: dict[str, str] = field(default_factory=dict)
    search_roots: list[str] = field(default_factory=lambda: list(DEFAULT_SEARCH_ROOTS))
    skip_names: list[str] = field(default_factory=lambda: list(DEFAULT_SKIP_NAMES))
    scratch_root: str = "~/Projects/_scratch/小E"
    max_parallel: int = 3
    auto_route: bool = True  # 直接说话时让小E判断接着哪个对话、还是新开
    default_effort: str | None = None  # 小E 对话的默认思考深度（low/medium/high/xhigh/max）；不填用 Claude Code 设置里的
    simple_model: str = "sonnet"  # 简单任务（查资料、整理文件、问答）用的模型
    dev_model: str = "opus"  # 开发类工作（写代码、做功能、做课件，不限哪个项目）用的模型
    web_base: str = ""  # 过程页与试课预览的外网地址（如 https://xiaoe.ericlab.cn）；空 = 不发链接
    web_port: int = 8765  # 本机网页服务端口，经 SSH 反向隧道接到阿里云
    preview_upstream: str = "http://127.0.0.1:8000"  # 试课预览转给本机哪个 CyberScope 后端
    preview_root: str = ""  # 该后端 /workspace/ 对应的课程根；改了这里面的课就附“试课预览”链接


def load_config(path: Path = CONFIG_PATH) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return Config(
        client_id=raw["client_id"],
        allowed_user_ids=[str(x) for x in raw.get("allowed_user_ids", [])],
        folders=dict(raw.get("folders", {})),
        search_roots=list(raw.get("search_roots", DEFAULT_SEARCH_ROOTS)),
        skip_names=list(raw.get("skip_names", DEFAULT_SKIP_NAMES)),
        scratch_root=raw.get("scratch_root", "~/Projects/_scratch/小E"),
        max_parallel=int(raw.get("max_parallel", 3)),
        auto_route=bool(raw.get("auto_route", True)),
        default_effort=raw.get("default_effort"),
        simple_model=raw.get("simple_model", "sonnet"),
        dev_model=raw.get("dev_model", "opus"),
        web_base=str(raw.get("web_base", "")).rstrip("/"),
        web_port=int(raw.get("web_port", 8765)),
        preview_upstream=str(raw.get("preview_upstream", "http://127.0.0.1:8000")).rstrip("/"),
        preview_root=str(raw.get("preview_root", "")),
    )


def read_secret(client_id: str) -> str:
    """从钥匙串取钉钉应用密钥；取不到时报错并说明怎么存。"""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", client_id, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(
            f"钥匙串里没有钉钉密钥（服务名 {KEYCHAIN_SERVICE}，账户 {client_id}）。"
            "运行 scripts/save_secret.sh 存一次。"
        )
    return r.stdout.strip()
