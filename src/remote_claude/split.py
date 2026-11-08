"""分工表：什么活交给谁。新事分三档（router.KINDS：design 定方案 / dev 开发 / simple 其余），每档写
首选（Claude 的型号 opus / sonnet …，或别家引擎 gpt / deepseek / kimi）、别家的具体型号、深度，和首选用不了时的备选。

新开对话、定时任务没点名模型时照它选；首选这家没装、没密钥、受限账号不能用、文件夹不许发给它（DeepSeek 禁区）时换备选，
备选也不行就退回 Claude Sonnet。质量优先（2026-11-08 定：不为省额度降质量）：首选这家额度只是偏紧照用首选；
告急或撞线（quota.py 按用量和速度分档，快干不了活了）才换备选，且备选那家要更宽裕（或同是 Claude、换成更省的型号）。配置写法见 config.example.toml 的 [split]；老写法 design_model / dev_model / simple_model 也认。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import engines, quota
from .engines import EFFORTS, MODELS

KINDS = {"design": "定方案", "dev": "开发", "simple": "简单任务"}
HELP = {"design": "做设计、梳理想法、写方案、架构设计、验收", "dev": "照清楚的要求写代码、做功能、改 bug",
        "simple": "查资料、整理文件、问答、小改动"}
FIELDS = ("use", "model", "effort", "backup")
DEFAULT = {"design": {"use": "opus"}, "dev": {"use": "sonnet"}, "simple": {"use": "sonnet"}}
LEGACY = {"design": "design_model", "dev": "dev_model", "simple": "simple_model"}   # 老写法的键
FALLBACK = "sonnet"
WEIGHT = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}   # Claude 各型号多耗额度（额度紧时同家换更省的）


def row(use: str = "", model: str = "", effort: str = "", backup: str = "") -> dict:
    return {"use": use, "model": model, "effort": effort, "backup": backup}


def normalize(raw: dict | None, legacy: dict | None = None) -> dict[str, dict]:
    """配置里的 [split] 表 → 每档齐全的 {use, model, effort, backup}。某档没写时看老写法（dev_model = "sonnet"），再没有用默认。"""
    raw, legacy = raw or {}, legacy or {}
    out = {}
    for kind in KINDS:
        r = raw.get(kind)
        if isinstance(r, str):   # [split] dev = "gpt" 这种简写
            r = {"use": r}
        if not isinstance(r, dict):
            r = {"use": legacy[LEGACY[kind]]} if legacy.get(LEGACY[kind]) else DEFAULT[kind]
        out[kind] = row(**{k: str(r.get(k) or "").strip() for k in FIELDS})
        out[kind]["use"] = out[kind]["use"] or DEFAULT[kind]["use"]
    return out


def picks() -> list[tuple[str, str]]:
    """首选、备选能填的值：Claude 的型号，再加上别家引擎的键。没装、没密钥的也列（先配好再装也行），标上“用不了”。"""
    return [(k, f"Claude {k.capitalize()}") for k in MODELS] + [
        (e.key, e.full if e.available() else f"{e.full}（这台电脑用不了）") for e in engines.known() if e.pick_model != "menu"]


def engine_models() -> dict[str, list[str]]:
    """别家能填的型号（设置页给候选）：Codex 启动时问到的、DeepSeek 的 flash / pro……"""
    return {e.key: e.models()[0] for e in engines.known() if e.pick_model == "own"}


def efforts() -> list[tuple[str, str]]:
    return [("", "默认")] + [(v, k) for k, v in EFFORTS.items() if v]


def check(table: dict) -> str:
    """不对就返回哪里不对（给设置页报错），对了返回空。"""
    ok = {k for k, _ in picks()}
    eff = {v for v, _ in efforts()}
    for kind, r in table.items():
        if kind not in KINDS:
            return f"不认得的档位：{kind}"
        name = KINDS[kind]
        if r.get("use") not in ok:
            return f"{name}的首选不在可选范围里"
        if r.get("backup") and r["backup"] not in ok:
            return f"{name}的备选不在可选范围里"
        if r.get("effort", "") not in eff:
            return f"{name}的深度不在可选范围里"
        if r.get("model") and r["use"] in MODELS:
            return f"{name}首选的是 Claude，不用另填型号（型号就是 {r['use']}）"
        if len(r.get("model", "")) > 60:
            return f"{name}的型号太长了"
    return ""


@dataclass
class Choice:
    pick: str            # 选中的：Claude 型号短名（opus…）或别家引擎的键（gpt…）
    model: str = ""      # 别家自己的型号（空 = 用它自己配置的）
    effort: str = ""     # 空 = 默认深度
    note: str = ""       # 首选没用上时的原因（开工卡片上告诉你）

    @property
    def engine(self) -> str | None:
        """要换成的引擎名；Claude 返回 None。"""
        return engines.engine_of(self.pick)

    @property
    def full_model(self) -> str | None:
        """Session.model 该填什么：Claude 填型号全名；别家（型号自己报的）填它的型号，没填就 None。"""
        if self.pick in MODELS:
            return MODELS[self.pick]
        return self.model or None


def usable(pick: str, paths: list, fenced: bool = False, cfg=None) -> str:
    """这个首选 / 备选现在能不能用；不能用返回原因。"""
    if not pick:
        return "没填"
    if pick in MODELS:
        return ""
    e = engines.by_key(pick)
    if not e:
        return "不认得"
    if not e.available():
        return f"{e.label} 没装或没密钥"
    if fenced and not e.fenced_ok:
        return "受限账号只用 Claude"
    if cfg is not None and (hit := e.refuses(cfg, paths)):
        return f"文件夹在“{hit}”里，不发给 {e.label}"
    return ""


def family(pick: str) -> str:
    """首选 / 备选算哪家的额度：Claude 的型号都算 claude，别家就是它的键。"""
    return engines.CLAUDE.key if pick in MODELS else pick


def _label(pick: str) -> str:
    e = engines.by_key(pick)
    return e.full if e and pick not in MODELS else f"Claude {pick.capitalize()}"


def _roomier(use: str, backup: str, levels: dict) -> bool:
    """首选这家额度紧时，备选是不是更好的去处：那家更宽裕，或同是 Claude 换成更省的型号。"""
    a, b = levels.get(family(use), "充足"), levels.get(family(backup), "充足")
    if b == "撞线":
        return False
    if family(use) == family(backup):
        return WEIGHT.get(backup, 9) < WEIGHT.get(use, 9)
    return quota.rank(b) < quota.rank(a)


def choose(cfg, kind: str | None, paths: list[Path | str] = (), fenced: bool = False, engine: str | None = None,
           levels: dict | None = None) -> Choice:
    """按分工表给这一档挑：首选能用就首选，不行换备选，再不行 Claude Sonnet。
    engine = 已经点名了哪家（总管写了 engine=claude 却没写型号）：只在这家里挑，不看额度。
    levels = 各家额度现在在哪一档（Bridge.quota_levels）：首选这家告急 / 撞线时，备选更宽裕就换备选（偏紧不换，质量优先）。"""
    table = getattr(cfg, "split", None) or normalize(None)
    r = table.get(kind or "simple") or table["simple"]
    if engine == engines.CLAUDE.key:
        return Choice(next((x for x in (r["use"], r["backup"]) if x in MODELS), FALLBACK), "", r["effort"])
    if engine:
        return Choice(engine, r["model"] if r["use"] == engine else "", r["effort"])
    levels = levels or {}
    why = usable(r["use"], list(paths), fenced, cfg)
    backup_ok = bool(r["backup"]) and not usable(r["backup"], list(paths), fenced, cfg)
    if not why:
        lv = levels.get(family(r["use"]), "充足")
        if lv in quota.SWITCH and backup_ok and _roomier(r["use"], r["backup"], levels):
            return Choice(r["backup"], "", r["effort"], note=f"{_label(r['use'])} 额度{lv}，换备选")
        if lv == "撞线" and not backup_ok and family(r["use"]) != family(FALLBACK) and levels.get(family(FALLBACK)) != "撞线":
            return Choice(FALLBACK, "", r["effort"], note=f"{_label(r['use'])} 额度撞线")
        return Choice(r["use"], "" if r["use"] in MODELS else r["model"], r["effort"])
    if backup_ok:
        return Choice(r["backup"], "", r["effort"], note=f"首选 {_label(r['use'])} 用不了（{why}），换备选")
    return Choice(FALLBACK, "", r["effort"], note=f"首选 {_label(r['use'])} 用不了（{why}）")


def describe(cfg) -> str:
    """一行说清分工表（给总管看）：定方案用 opus，开发用 gpt（备选 sonnet），其余用 sonnet。"""
    table = getattr(cfg, "split", None) or normalize(None)
    parts = []
    for kind, name in KINDS.items():
        r = table[kind]
        extra = "、".join(x for x in (r["model"], f"深度 {r['effort']}" if r["effort"] else "",
                                      f"备选 {r['backup']}" if r["backup"] else "") if x)
        parts.append(f"{name}用 {r['use']}" + (f"（{extra}）" if extra else ""))
    return "，".join(parts)
