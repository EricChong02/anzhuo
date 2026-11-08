"""网页里的“设置”：把 config.toml 里常改的那些项摆出来改，改完写回 config.toml（保留注释和没列出来的项）。

一张表（FIELDS）说清每一项：叫什么、什么类型、怎么校验、改了能不能马上生效、放在哪组、是不是“高级”、跟着哪个开关显示。
网页、校验、保存、生效都照这张表来，加一项设置只改这一处。分组按“用户想做什么”排（模型、回复、总管……），不按配置键排；
平时不用动的调参项（接力线、并发数、搜索范围）归“高级”，默认收起。config.toml 仍是唯一的配置来源；账号在“账号”页管，网页地址、端口、钉钉这些连接类的不在这里改。
"""
from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import tomlkit

from .config import CONFIG_PATH, Config, load_config
from . import split
from .engines import MODELS
from .report import model_name

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
    rng: tuple = (1, 10)   # int 的取值范围
    check: Callable[[Any], None] | None = None
    adv: bool = False   # True = 放进默认收起的“高级”
    when: str = ""   # 只在这个开关（设置项或配置项，比如 dingtalk_enabled）打开时显示


def _models() -> tuple:
    """存短名（opus），下拉里显示正式名（Claude Opus 5.5）。"""
    return tuple((k, "Claude " + model_name(v)) for k, v in MODELS.items())


def _efforts() -> tuple:
    return (("", "跟 Claude Code 设置"), ("low", "低"), ("medium", "中"), ("high", "高"), ("xhigh", "很高"), ("max", "最高"))


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


def _split_check(v: dict):
    if why := split.check(v):
        raise Invalid(why)


def _folders(v: dict):
    for name, path in v.items():
        if not Path(path).expanduser().is_dir():
            raise Invalid(f"“{name}”的文件夹不存在：{path}")


FIELDS: list[Field] = [
    Field("split", "分工表", "模型", "split",
          "新对话没点名模型时，按活的类型选。首选用不了（没装、没密钥、受限账号、文件夹不许发给它）换备选，再不行用 Claude Sonnet",
          check=_split_check),
    Field("subagent_model", "子代理", "模型", "select", "对话里派出去帮忙的子代理用哪个模型", options=()),
    Field("default_effort", "思考深度", "模型", "select", "新对话默认想多深；对话里可以单独改", options=()),
    Field("reply_language", "回复语言", "回复", "text", "留空不指定", check=_short(20)),
    Field("extra_instructions", "固定要求", "回复", "textarea", "每个对话都会照做，比如“小改动做完直接提交，不用问我”", check=_short(LONG)),
    Field("translate_english", "长段英文译成中文", "回复", "bool", "有 DeepSeek 密钥用它，没有用 Haiku（占你的额度）"),
    Field("super_agent", "总管", "总管", "bool", "多一个置顶对话：你只跟它说，它替你开对话、派活、汇总", live=False),
    Field("super_agent_name", "名字", "总管", "text", "留空叫“总管”", check=_short(30), when="super_agent"),
    Field("super_agent_routing", "例外的分派规则", "总管", "textarea",
          "平时照“模型分工”选；只有例外才写，一行一条，比如“查资料：kimi”", check=_short(LONG), when="super_agent"),
    Field("folders", "常用文件夹", "文件夹", "folders", "新对话菜单和“新开 短名：…”用它。一行一个：短名 = 路径", check=_folders),
    Field("deepseek_block", "不让 DeepSeek 碰的文件夹", "文件夹", "lines", "一行一个文件夹名（前缀匹配）：在这里面不开 DeepSeek，DeepSeek 对话也读不到"),
    Field("auto_review", "AI 审核危险命令", "安全", "bool", "关着时删文件、git push 这类都等你点“允许”"),
    Field("review_policy", "审核员可以放行的事", "安全", "textarea", "比如“推送到 dev 分支可以放行”", check=_short(LONG), when="auto_review"),
    Field("task_grouping", "按任务自动分类", "自动", "bool", "会把标题、文件夹名和最多 400 字摘要发给模型"),
    Field("task_group_rules", "分类规则", "自动", "textarea", "一行一条，比如“同一门课的各小节都归到课程名下”", check=_short(LONG), when="task_grouping"),
    Field("auto_cleanup", "每天自动整理", "自动", "bool", "归档旧记录、清旧临时文件夹，每月一次体检", live=False),
    Field("dingtalk_quiet", "钉钉只发结果", "自动", "bool", "开工、进度这些只在网页里看", when="dingtalk_enabled"),
    Field("relay_long", "对话多长提醒接力", "高级", "int", "单位 token，默认 120000；越长每句话越贵", rng=(30_000, 1_000_000), adv=True),
    Field("relay_cold", "隔一小时回来、多长提醒接力", "高级", "int", "缓存过期后整段要重读，单位 token", rng=(10_000, 1_000_000), adv=True),
    Field("max_parallel", "最多同时干活的对话", "高级", "int", "多的排队", live=False, adv=True),
    Field("scratch_root", "临时文件夹放哪", "高级", "text", "没指定文件夹时在这里新建", check=_short(500), adv=True),
    Field("search_roots", "按名字找文件夹的范围", "高级", "lines", "一行一个路径", adv=True),
    Field("skip_names", "找文件夹时跳过", "高级", "lines", "一行一个文件夹名（前缀匹配），比如放私密资料的", adv=True),
    Field("timezone", "定时任务的时区", "高级", "text", "比如 Asia/Shanghai；留空用电脑当前时区", check=_tz, adv=True),
]
BY_KEY = {f.key: f for f in FIELDS}


def _options(f: Field) -> tuple:
    if f.key == "subagent_model":
        return (("", "跟主对话"),) + _models()
    if f.key == "default_effort":
        return _efforts()
    return f.options


def values(cfg: Config) -> dict:
    out = {}
    for f in FIELDS:
        v = getattr(cfg, f.key)
        out[f.key] = "" if v is None else v
    for k in {f.when for f in FIELDS if f.when} - set(BY_KEY):   # 跟着配置项显示的（钉钉开没开）：只读，给网页判断
        out[k] = bool(getattr(cfg, k))
    return out


def schema() -> list[dict]:
    out = []
    for f in FIELDS:
        d = {"key": f.key, "label": f.label, "group": f.group, "kind": f.kind, "help": f.help, "live": f.live,
             "options": [list(o) for o in _options(f)],
             "adv": f.adv, "when": f.when}
        if f.kind == "int":
            d["range"] = list(f.rng)
        if f.kind == "split":   # 分工表：三档 × 首选 / 型号 / 深度 / 备选
            d["rows"] = [[k, name, split.HELP[k]] for k, name in split.KINDS.items()]
            d["picks"] = [list(o) for o in split.picks()]
            d["efforts"] = [list(o) for o in split.efforts()]
            d["engine_models"] = split.engine_models()
        out.append(d)
    return out


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
        lo, hi = f.rng
        if not lo <= v <= hi:
            raise Invalid(f"填 {lo} 到 {hi}")
        return v
    if f.kind == "lines":
        items = raw if isinstance(raw, list) else str(raw or "").splitlines()
        return [str(x).strip() for x in items if str(x).strip()]
    if f.kind == "split":
        if not isinstance(raw, dict):
            raise Invalid("格式不对")
        bad = [k for k in raw if k not in split.KINDS]
        if bad:
            raise Invalid(f"不认得的档位：{bad[0]}")
        return split.normalize({k: v for k, v in raw.items() if isinstance(v, dict)})
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
    if "split" in changes:
        for old in split.LEGACY.values():
            if old in doc:
                del doc[old]
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
        elif key == "split":   # [split.dev] use = "gpt" …；空的项不写。老写法 dev_model 这些一并删掉，免得两处打架
            t = tomlkit.table(is_super_table=True)
            for kind, r in v.items():
                sub = tomlkit.table()
                for k, x in r.items():
                    if x:
                        sub[k] = x
                t[kind] = sub
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
    bridge.cfg = dataclasses.replace(new, **keep)   # Runner 跟着换（Bridge.cfg），回复语言、审核员这些下一轮就用新的
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
