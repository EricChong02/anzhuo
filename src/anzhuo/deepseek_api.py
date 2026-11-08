"""直连 DeepSeek 的对话接口（不经 Claude Code）：远窗自己的小活——翻译、任务分组、判断任务轻重——先用它，
约 1 秒、几乎不花钱、不占 Claude 额度。拿不到密钥或调用出错，由调用方退回 Haiku。
密钥只放钥匙串（服务 anzhuo.deepseek，账户 api），和 DeepSeek 引擎共用，不写配置文件。"""
from __future__ import annotations

import subprocess
import time

import requests

URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"  # DeepSeek-V4.1-Flash
KEYCHAIN = ("anzhuo.deepseek", "api")  # 钥匙串服务名、账户

_key_cache: tuple[float, str | None] = (0.0, None)


def key() -> str | None:
    """钥匙串里的密钥（一分钟内不重复读）；没存或不是 macOS 返回 None。"""
    global _key_cache
    if time.time() - _key_cache[0] > 60:
        service, account = KEYCHAIN
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                               capture_output=True, text=True)
            _key_cache = (time.time(), r.stdout.strip() or None)
        except OSError:   # 没有 security 命令
            _key_cache = (time.time(), None)
    return _key_cache[1]


def chat(system: str, user: str, api_key: str, *, timeout: float, temperature: float = 0.2, **extra) -> str:
    """一问一答，不开思考；extra 原样放进请求（如 response_format、max_tokens）。出错直接抛。"""
    r = requests.post(URL, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}, json={
        "model": MODEL, "thinking": {"type": "disabled"}, "temperature": temperature, **extra,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    })
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""
