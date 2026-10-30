"""配置与密钥：配置文件在 ~/.config/anzhuo/config.toml，钉钉应用密钥和网页密码指纹只存在 macOS 钥匙串。"""
from __future__ import annotations

import os
import subprocess
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
    web_user: str = ""  # 网页登录账号；密码只以指纹存钥匙串（anzhuo passwd）
    watch_host: str = ""  # 可选：外网服务器的 ssh 别名，那边每分钟查案头在不在线，掉线发手机通知（scripts/server_watch.py）
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
    translate_english: bool = True  # 回复里有大段英文时译成中文再给你看（reply_language 不是中文时请关掉）
    # ---- 安全与整理 ----
    auto_review: bool = False  # 危险命令交给 AI 审核员判断；false = 都等你点“允许”（默认）
    review_policy: str = ""  # 你额外授权审核员放行的事（比如“推送到我自己仓库的 dev 分支可以放行”）
    auto_cleanup: bool = False  # 每天自动归档旧对话记录、清空旧临时文件夹，每月派一次 AI 体检（见 docs/配置参考.md）
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
    return Config(
        client_id=str(raw.get("client_id", "")),
        allowed_user_ids=[str(x) for x in raw.get("allowed_user_ids", [])],
        web_base=web_base,
        web_port=port,
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
        translate_english=bool(raw.get("translate_english", True)),
        auto_review=bool(raw.get("auto_review", False)),
        review_policy=str(raw.get("review_policy", "")),
        auto_cleanup=bool(raw.get("auto_cleanup", False)),
        timezone=str(raw.get("timezone", "")),
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


def read_web_hash(user: str) -> str:
    """网页登录密码的指纹（PBKDF2）：先看环境变量 ANZHUO_WEB_HASH（演示、测试用），再读钥匙串；都没有就返回空（网页谁也进不去）。"""
    if not user:
        return ""
    if os.environ.get("ANZHUO_WEB_HASH"):
        return os.environ["ANZHUO_WEB_HASH"]
    r = subprocess.run(["security", "find-generic-password", "-s", WEB_KEYCHAIN, "-a", user, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip()
