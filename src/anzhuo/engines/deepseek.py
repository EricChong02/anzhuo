"""DeepSeek 引擎：还是 Claude Code（工具、钩子、交图交文件、回到这里都照旧），只是把后端换成 DeepSeek 的
Anthropic 兼容接口（https://api.deepseek.com/anthropic）。按量计费、极便宜，不占 Claude 的 5 小时和周额度。

- 密钥只放钥匙串（服务 anzhuo.deepseek，账户 api；和翻译共用），不写配置文件。
- 发给它的内容会出境到 DeepSeek：工作文件夹在 deepseek_block 列的文件夹里就不开工；
  干活中途碰到这些文件夹的工具调用一律拒掉。受限账号用不了它（fenced_ok = False）。
- DeepSeek 忽略 cache_control（它自己自动缓存），不处理 document 块；deepseek-v4-pro 看不了图。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import HookMatcher

from .claude import ClaudeEngine

if TYPE_CHECKING:
    from ..report import Turn
    from ..sessions import Job, Runner, Session

BASE_URL = "https://api.deepseek.com/anthropic"
FLASH, PRO = "deepseek-flash", "deepseek-v4-pro"
# 元 / 百万 token（闲时价，2026-11-08 查；工作日 9–12、14–18 点翻倍）。记消耗时折成美元，只是个估数
PRICE_CNY = {FLASH: {"in": 1.0, "cached": 0.02, "out": 4.0}, PRO: {"in": 4.0, "cached": 0.1, "out": 16.0}}
CNY_PER_USD = 7.1


def api_key() -> str | None:
    """钥匙串里的 DeepSeek 密钥（和翻译、任务分组共用，见 deepseek_api）。"""
    from ..deepseek_api import key
    return key()


def blocked_by(paths: list[str], names: list[str]) -> str:
    """这些路径里有没有不许发给 DeepSeek 的文件夹（按文件夹名前缀）；有就返回那个名字。"""
    for p in paths:
        for part in Path(p).expanduser().parts:
            if hit := next((n for n in names if n and part.startswith(n)), ""):
                return hit
    return ""


def cost_usd(models: dict) -> float:
    """Claude Code 报的是按 Claude 价算的金额，DeepSeek 要按它自己的价重算（会话累计，和 Claude 一样相减）。"""
    total = 0.0
    for name, u in (models or {}).items():
        p = PRICE_CNY[PRO if "pro" in name else FLASH]
        fresh = int(u.get("inputTokens") or 0) + int(u.get("cacheCreationInputTokens") or 0)
        total += (fresh * p["in"] + int(u.get("cacheReadInputTokens") or 0) * p["cached"]
                  + int(u.get("outputTokens") or 0) * p["out"]) / 1e6
    return total / CNY_PER_USD


class DeepSeekEngine(ClaudeEngine):
    name, label, tool, key = "deepseek", "DeepSeek", "Claude Code", "deepseek"
    pick_model = "own"
    has_quota = fenced_ok = cache_hint = False   # 按量计费没有额度窗口；受限账号不用；它自动缓存、不按小时过期
    context_window = 1_000_000   # V4 两档都是 1M 上下文；Claude Code 不认识这型号，报的百分比按 200K 算会虚高五倍

    def available(self) -> bool:
        return bool(api_key())

    def refuses(self, cfg, paths: list) -> str:
        return blocked_by([str(p) for p in paths], list(getattr(cfg, "deepseek_block", []) or []))

    def models(self) -> tuple[list[str], str]:
        return [FLASH, PRO], FLASH

    def limits(self, runner: "Runner") -> dict:
        return {}

    async def refresh_quota(self, runner: "Runner", force: bool = False) -> None:
        pass

    def model_for(self, s: "Session") -> str | None:
        return s.model if s.model in (FLASH, PRO) else FLASH

    def env(self, runner: "Runner", s: "Session") -> dict:
        key = api_key()
        if not key:
            raise RuntimeError("钥匙串里没有 DeepSeek 密钥（服务 anzhuo.deepseek，账户 api），先存一次再用")
        model = self.model_for(s)
        return {"ENABLE_TOOL_SEARCH": "false",
                "ANTHROPIC_BASE_URL": BASE_URL, "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": FLASH, "CLAUDE_CODE_SUBAGENT_MODEL": FLASH,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

    def extra_hooks(self, runner: "Runner", s: "Session") -> list:
        names = list(getattr(runner, "deepseek_block", []) or [])
        if not names:
            return []

        async def hook(data, tool_use_id, context):
            raw = json.dumps(data.get("tool_input") or {}, ensure_ascii=False)
            if hit := next((n for n in names if n and n in raw), ""):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": f"这个对话跑在 DeepSeek 上（内容出境），不能碰“{hit}”里的资料。"
                                                                           "需要的话请用户换 Claude 来做。"}}
            return {}
        return [HookMatcher(matcher=None, hooks=[hook], timeout=60)]

    async def run(self, runner: "Runner", s: "Session", job: "Job", turn: "Turn") -> None:
        if hit := blocked_by([s.cwd, *s.add_dirs], list(getattr(runner, "deepseek_block", []) or [])):
            raise RuntimeError(f"工作文件夹在“{hit}”里，资料不发给 DeepSeek；请换 Claude 来做")
        await super().run(runner, s, job, turn)
        if turn.usage and turn.usage.get("models"):
            turn.usage["cost"] = round(cost_usd(turn.usage["models"]), 6)
