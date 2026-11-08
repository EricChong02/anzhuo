"""网页里的“设置”：把 config.toml 里常改的那些项摆出来改，改完写回 config.toml（保留注释和没列出来的项）。

一张表（FIELDS）说清每一项：叫什么、什么类型、怎么校验、改了能不能马上生效。网页、校验、保存、生效都照这张表来，
加一项设置只改这一处。config.toml 仍是唯一的配置来源；账号在“账号”页管，网页地址、端口、钉钉这些连接类的不在这里改。
"""
from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import tomlkit

from .config import CONFIG_PATH, Config, load_config

if TYPE_CHECKING:
    from .app import Bridge

log = logging.getLogger(__name__)

LONG = 4000   # 大段文字最多多少字


class Invalid(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str  # bool / text / textarea / int / select / lines / folders
    help: str = ""
    options: tuple = ()   # select 的可选值：((值, 显示), ...)
    live: bool = True   # False = 存下来，重启远窗后生效
    check: Callable[[Any], None] | None = None


def _models() -> tuple:
    from .app import MODELS
    return tuple((k, k) for k in MODELS)


def _efforts() -> tuple:
    return (("", "跟 Claude Code 设置走"), ("low", "低"), ("medium", "中"), ("high", "高"), ("xhigh", "很高"), ("max", "最高"))


def _short(limit: int):
    def check(v):
        if len(v) > limit:
            raise Invalid(f"太长了，最多 {limit} 字")
    return check


def _tz(v):
    if v:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception as e:  # noqa: BLE001
            raise Invalid("不认得这个时区，写成 Asia/Shanghai 这样，或留空用电脑当前时区") from e


def _folders(v: dict):
    for name, path in v.items():
        if not Path(path).expanduser().is_dir():
            raise Invalid(f"“{name}”的文件夹不存在：{path}")


FIELDS: list[Field] = [
    Field("super_agent", "打开总管", "总管", "bool", "多一个置顶的总管对话：你只跟它说，它开对话派活、转达、汇总回报", live=False),
    Field("super_agent_name", "总管的名字", "总管", "text", "留空叫“总管”", check=_short(30)),
    Field("super_agent_routing", "分派规则", "总管", "textarea",
          "你没点名用哪个引擎 / 模型时，总管照这个选。一行一条，比如“写代码：claude，模型 sonnet”", check=_short(LONG)),
    Field("simple_model", "简单任务用的模型", "干活", "select", "查资料、整理文件、问答", options=()),
    Field("dev_model", "开发任务用的模型", "干活", "select", "文件夹里有 .git / pyproject.toml / package.json 时", options=()),
    Field("default_effort", "默认思考深度", "干活", "select", options=()),
    Field("max_parallel", "最多同时干活的对话数", "干活", "int", "多的排队", live=False),
    Field("reply_language", "回复语言", "干活", "text", "留空 = 不指定", check=_short(20)),
    Field("extra_instructions", "给每个对话的固定要求", "干活", "textarea", "比如“小改动做完直接提交，不用问我”", check=_short(LONG)),
    Field("folders", "文件夹短名", "文件夹", "folders", "一行一个：短名 = 路径。新对话菜单和“新开 短名：…”都用它", check=_folders),
    Field("scratch_root", "临时文件夹放哪", "文件夹", "text", "没指定文件夹时用", check=_short(500)),
    Field("search_roots", "按名字找文件夹的范围", "文件夹", "lines", "一行一个路径"),
    Field("skip_names", "找文件夹时跳过", "文件夹", "lines", "一行一个文件夹名（前缀匹配），比如放私密资料的"),
    Field("auto_review", "危险命令交给 AI 审核员", "安全", "bool", "关着时删文件、git push 这类都等你点“允许”"),
    Field("review_policy", "审核员可以放行的事", "安全", "textarea", "打开审核员后才有用，比如“推送到 dev 分支可以放行”", check=_short(LONG)),
    Field("translate_english", "大段英文译成中文", "其他", "bool", "有 DeepSeek 密钥用它，没有用 Haiku（花你的额度）"),
    Field("task_grouping", "按任务自动分类", "其他", "bool", "会把标题、文件夹名和最多 400 字摘要发给模型"),
    Field("auto_cleanup", "自动整理", "其他", "bool", "每天归档旧记录、清旧临时文件夹，每月一次体检", live=False),
    Field("dingtalk_quiet", "钉钉只发结果", "其他", "bool", "开工、进度这些只在网页里看"),
    Field("timezone", "定时任务的时区", "其他", "text", "比如 Asia/Shanghai；留空用电脑当前时区", check=_tz),
]
BY_KEY = {f.key: f for f in FIELDS}


def _options(f: Field) -> tuple:
    if f.key in ("simple_model", "dev_model"):
        return _models()
    if f.key == "default_effort":
        return _efforts()
    return f.options


def values(cfg: Config) -> dict:
    out = {}
    for f in FIELDS:
        v = getattr(cfg, f.key)
        out[f.key] = "" if v is None else v
    return out


def schema() -> list[dict]:
    return [{"key": f.key, "label": f.label, "group": f.group, "kind": f.kind, "help": f.help, "live": f.live,
             "options": [list(o) for o in _options(f)]} for f in FIELDS]


def parse(data: dict, current: Config) -> dict:
    """网页交来的值 → 校验过、类型对的值（只要改了的、认得的项）。不对就抛 Invalid（带是哪一项）。"""
    out = {}
    for key, raw in (data or {}).items():
        f = BY_KEY.get(key)
        if not f:
            continue
        try:
            v = _coerce(f, raw)
            if f.kind == "select" and v not in [o[0] for o in _options(f)]:
                raise Invalid("不在可选范围里")
            if f.check:
                f.check(v)
        except Invalid as e:
            raise Invalid(f"{f.label}：{e}") from None
        if v != (getattr(current, key) if getattr(current, key) is not None else ""):
            out[key] = v
    return out


def _coerce(f: Field, raw):
    if f.kind == "bool":
        if not isinstance(raw, bool):
            raise Invalid("要么开要么关")
        return raw
    if f.kind == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise Invalid("要填数字") from None
        if not 1 <= v <= 10:
            raise Invalid("填 1 到 10")
        return v
    if f.kind == "lines":
        items = raw if isinstance(raw, list) else str(raw or "").splitlines()
        return [str(x).strip() for x in items if str(x).strip()]
    if f.kind == "folders":
        if isinstance(raw, dict):
            pairs = raw.items()
        else:
            pairs = []
            for ln in str(raw or "").splitlines():
                if not ln.strip():
                    continue
                if "=" not in ln:
                    raise Invalid(f"这一行要写成“短名 = 路径”：{ln.strip()}")
                pairs.append(tuple(x.strip() for x in ln.split("=", 1)))
        out = {}
        for k, v in pairs:
            if not k or not v:
                raise Invalid("短名和路径都不能空")
            out[str(k)] = str(v)
        return out
    return str(raw if raw is not None else "").strip()


def save(path: Path, changes: dict) -> None:
    """改动写回 config.toml：保留注释和别的项；值变回默认（空）就删掉那一行，让默认值接管。先写临时文件再换，写一半不会坏。"""
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    defaults = Config()
    for key, v in changes.items():
        default = getattr(defaults, key)
        if v == (default if default is not None else "") and key in doc:
            del doc[key]
            continue
        if key == "folders":
            t = tomlkit.table()
            for k, p in v.items():
                t[k] = p
            doc[key] = t
        elif isinstance(v, str) and "\n" in v:
            doc[key] = tomlkit.string(v if v.endswith("\n") else v + "\n", multiline=True)
        else:
            doc[key] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def apply(bridge: "Bridge", new: Config) -> None:
    """能马上生效的就让它生效：换上新配置，同步运行中各处拷过去的值。要重启的那几项只存不动。"""
    old = bridge.cfg
    keep = {f.key: getattr(old, f.key) for f in FIELDS if not f.live}   # 要重启的：运行中保持旧值，免得一半生效
    bridge.cfg = dataclasses.replace(new, **keep)
    r = bridge.runner
    r.language = new.reply_language
    r.extra_instructions = new.extra_instructions
    r.auto_review = new.auto_review
    r.review_policy = new.review_policy
    from .sessions import user_setting
    r.default_effort = new.default_effort or user_setting("effortLevel") or ""
    from . import translate, schedules
    translate.enabled = new.translate_english
    schedules.set_timezone(new.timezone)
    if new.super_agent_name != old.super_agent_name and old.super_agent:
        from . import superagent
        if router := superagent.find(bridge):
            router.name = (new.super_agent_name or superagent.NAME).strip()[:30]
            bridge.registry.save()


def pending(bridge: "Bridge", path: Path) -> list[str]:
    """存了但要重启才生效的项（配置文件里的和运行中的不一样）。"""
    try:
        on_disk = load_config(path)
    except Exception:  # noqa: BLE001 - 读不了就不报
        return []
    return [f.label for f in FIELDS if not f.live and getattr(on_disk, f.key) != getattr(bridge.cfg, f.key)]


def payload(bridge: "Bridge", path: Path = CONFIG_PATH) -> dict:
    try:
        shown = load_config(path) if path.exists() else bridge.cfg   # 显示文件里的（含还没生效的）
    except Exception:  # noqa: BLE001
        shown = bridge.cfg
    return {"fields": schema(), "values": values(shown), "restart": pending(bridge, path), "path": str(path)}


def update(bridge: "Bridge", data: dict, path: Path = CONFIG_PATH) -> dict:
    try:
        current = load_config(path) if path.exists() else bridge.cfg
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"现在的 config.toml 读不了：{e}"}
    try:
        changes = parse(data, current)
    except Invalid as e:
        return {"ok": False, "error": str(e)}
    if changes:
        save(path, changes)
        log.info("设置改了：%s", "、".join(BY_KEY[k].label for k in changes))   # 不记值（可能有私密路径、要求）
        apply(bridge, load_config(path))
    return {"ok": True, "changed": [BY_KEY[k].label for k in changes], "restart": pending(bridge, path)}
