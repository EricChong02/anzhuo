"""引擎：Claude / GPT（Codex）/ DeepSeek（Claude Code 接 DeepSeek 后端）/ ACP（Kimi 等）各是一个 Engine 实现。哪个对话用什么、能做什么，都查这里。"""
from .base import COMPACT, CONFIRM_WAIT, Engine, FolderBlocked
from .catalog import CLAUDE, CODEX, DEEPSEEK, EFFORTS, HELPER_MODEL, MODELS, by_key, can_switch_model, engine_of, get, is_acp, known, switches

__all__ = ["COMPACT", "CONFIRM_WAIT", "Engine", "FolderBlocked", "CLAUDE", "CODEX", "DEEPSEEK", "EFFORTS", "HELPER_MODEL", "MODELS", "by_key",
           "can_switch_model", "engine_of", "get", "is_acp", "known", "switches"]
