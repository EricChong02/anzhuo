"""配置与密钥：配置文件在 ~/.config/anzhuo/config.toml，钉钉应用密钥存 macOS 钥匙串；
网页密码指纹 macOS 存钥匙串（Windows / Linux 存配置目录旁的 web_hash.json）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("ANZHUO_CONFIG", HOME / ".config/anzhuo/config.toml"))
DATA_DIR = Path(os.environ.get("ANZHUO_DATA", HOME / ".local/share/anzhuo"))
KEYCHAIN_SERVICE = "anzhuo.dingtalk"

DEFAULT_SEARCH_ROOTS = ["~/Projects", "~/Desktop", "~/Documents", "~/Downloads"]


@dataclass
class Config:
    # ---- 入口：网页（默认开）和钉钉（可选）----
    client_id: str = ""  # 钉钉应用的 Client ID；不填 = 不用钉钉，只用网页
    allowed_user_ids: list[str] = field(default_factory=list)  # 只认这些钉钉 ID 发来的单聊消息
    web_base: str = ""  # 网页的访问地址（http://127.0.0.1:8765，或你放在反向代理后面的外网地址）；空 = 不开网页
    web_port: int = 8765  # 网页只听本机这个端口
    web_user: str = ""  # 网页登录账号；密码只以指纹存钥匙串，非 macOS 存 web_hash.json（anzhuo passwd）
    web_prefix: str = ""  # 子路径挂载：被别的面板反代透传（不剥前缀）时整站挂在它下面，如 "/yuanchuang"；空 = 根路径
    watch_host: str = ""  # 可选：外网服务器的 ssh 别名，那边每分钟查远窗在不在线，掉线发手机通知（scripts/server_watch.py）
    dingtalk_quiet: bool = True  # 钉钉只发结果（做完/等你定/出错、回答）；开工、收到、插话、工作台这些过程只在网页里看
    # ---- 文件夹 ----
    folders: dict[str, str] = field(default_factory=dict)  # 短名 → 路径；网页新对话的菜单和“新开 短名：…”都用它
    search_roots: list[str] = field(default_factory=lambda: list(DEFAULT_SEARCH_ROOTS))  # “新开 某个词：…”按名字找文件夹的范围
    skip_names: list[str] = field(default_factory=list)  # 按名字找文件夹时跳过的文件夹名（前缀匹配），比如放私密资料的
    scratch_root: str = "~/anzhuo-scratch"  # “不指定文件夹”时的临时文件夹放哪
    # ---- 干活的方式 ----
    max_parallel: int = 3
    default_effort: str | None = None  # 默认思考深度（low/medium/high/xhigh/max）；不填用 Claude Code 设置里的
    simple_model: str = "sonnet"  # 简单任务（查资料、整理文件、问答）用的模型
    dev_model: str = "opus"  # 开发类工作（文件夹里有 .git / pyproject.toml / package.json）用的模型
    reply_language: str = "简体中文"  # 让每个对话用什么语言回复；写成空字符串 = 不指定
    extra_instructions: str = ""  # 你自己追加给每个对话的固定要求（比如“做完小任务直接提交，不要问”）
    translate_english: bool = False  # 默认关闭。打开后，回复里有大段英文时译成中文再给你看：有 DeepSeek 密钥就用它，没有就用 Haiku
    # ---- 安全与整理 ----
    auto_review: bool = False  # 危险命令交给 AI 审核员判断；false = 都等你点“允许”（默认）
    review_policy: str = ""  # 你额外授权审核员放行的事（比如“推送到我自己仓库的 dev 分支可以放行”）
    auto_cleanup: bool = False  # 每天自动归档旧对话记录、清空旧临时文件夹，每月派一次 AI 体检（见 docs/配置参考.md）
    accounts: dict[str, dict] = field(default_factory=dict)
    task_grouping: bool = False  # opt-in: classification sends short metadata to a model
    timezone: str = ""  # 定时任务用的时区（如 Asia/Shanghai）；空 = 这台电脑当前的时区

    @property
    def dingtalk_enabled(self) -> bool:
        return bool(self.client_id)


def load_config(path: Path = CONFIG_PATH) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    port = int(raw.get("web_port", 8765))
    # 网页默认开在本机：地址不写就用 http://127.0.0.1:端口；写 web = false 才关（那就必须用钉钉）
    web_base = str(raw.get("web_base", "")).rstrip("/") or (f"http://127.0.0.1:{port}" if raw.get("web", True) else "")
    # 子路径挂载：写成 "yuanchuang" 也当 "/yuanchuang"，结尾的 / 去掉
    web_prefix = str(raw.get("web_prefix", "")).strip()
    if web_prefix and not web_prefix.startswith("/"):
        web_prefix = "/" + web_prefix
    web_prefix = web_prefix.rstrip("/")
    return Config(
        client_id=str(raw.get("client_id", "")),
        allowed_user_ids=[str(x) for x in raw.get("allowed_user_ids", [])],
        web_base=web_base,
        web_port=port,
        web_prefix=web_prefix,
        web_user=str(raw.get("web_user", "")),
        watch_host=str(raw.get("watch_host", "")),
        dingtalk_quiet=bool(raw.get("dingtalk_quiet", True)),
        folders=dict(raw.get("folders", {})),
        search_roots=list(raw.get("search_roots", DEFAULT_SEARCH_ROOTS)),
        skip_names=list(raw.get("skip_names", [])),
        scratch_root=str(raw.get("scratch_root", "~/anzhuo-scratch")),
        max_parallel=int(raw.get("max_parallel", 3)),
        default_effort=raw.get("default_effort"),
        simple_model=raw.get("simple_model", "sonnet"),
        dev_model=raw.get("dev_model", "opus"),
        reply_language=str(raw.get("reply_language", "简体中文")),
        extra_instructions=str(raw.get("extra_instructions", "")),
        translate_english=bool(raw.get("translate_english", False)),
        auto_review=bool(raw.get("auto_review", False)),
        review_policy=str(raw.get("review_policy", "")),
        auto_cleanup=bool(raw.get("auto_cleanup", False)),
        timezone=str(raw.get("timezone", "")),
        accounts=dict(raw.get("accounts", {})),
        task_grouping=bool(raw.get("task_grouping", False)),
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


WEB_KEYCHAIN = "anzhuo.web"


def _web_hash_path() -> Path:
    """非 macOS 没有钥匙串：密码指纹存配置目录旁的 web_hash.json。"""
    return CONFIG_PATH.parent / "web_hash.json"


def read_web_hash(user: str) -> str:
    """网页登录密码的指纹（PBKDF2）：先看环境变量 ANZHUO_WEB_HASH（演示、测试用），再读钥匙串；
    非 macOS（Windows / Linux）读配置目录旁的 web_hash.json；都没有就返回空（网页谁也进不去）。"""
    if not user:
        return ""
    if os.environ.get("ANZHUO_WEB_HASH"):
        return os.environ["ANZHUO_WEB_HASH"]
    if sys.platform == "darwin":
        r = subprocess.run(["security", "find-generic-password", "-s", WEB_KEYCHAIN, "-a", user, "-w"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    try:
        store = json.loads(_web_hash_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return store.get(user, "") if isinstance(store, dict) else ""


def write_web_hash(user: str, pw_hash: str) -> None:
    """把网页登录密码的指纹存起来：macOS 存钥匙串，其它平台存 web_hash.json。"""
    if sys.platform == "darwin":
        subprocess.run(["security", "add-generic-password", "-U", "-s", WEB_KEYCHAIN, "-a", user, "-w", pw_hash],
                       check=True)
        return
    path = _web_hash_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = {}
    if not isinstance(store, dict):
        store = {}
    store[user] = pw_hash
    # 指纹虽不是明文，也只给自己读（Windows 上 chmod 只管只读位，靠用户目录本身的权限）
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(store))
    os.chmod(path, 0o600)


def web_accounts(cfg: Config) -> dict[str, dict]:
    accounts = cfg.accounts or ({cfg.web_user: {"role": "admin", "root": ""}} if cfg.web_user else {})
    if accounts and sum(a.get("role") == "admin" for a in accounts.values()) != 1:
        raise ValueError("accounts 必须有且只有一个 admin")
    out = {}
    for name, a in accounts.items():
        role = "admin" if a.get("role") == "admin" else "user"
        if role == "user" and (sys.platform != "darwin" or not a.get("root")):
            raise ValueError("受限账号仅支持 macOS，且必须设置独立 root")
        share = float(a.get("share", 0.2))
        if not 0 < share <= 1:
            raise ValueError("账号 share 必须大于0且不超过1")
        out[name] = {**a, "role": role, "share": share, "hash": read_web_hash(name)}
    return out
