"""小E：在钉钉里给 Mac 上的 Claude Code 派活、收报告。

  remote-claude run      常驻运行（开机自启用这个）
  remote-claude check    核对配置、钥匙串、钉钉、Claude 登录
  remote-claude whoami   临时运行，打印给小E发消息的人的钉钉 ID（首次配置用）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

from .config import CONFIG_PATH, DATA_DIR, load_config, read_secret


def _claude_logged_in() -> str:
    import claude_agent_sdk
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    r = subprocess.run([str(cli), "auth", "status"], capture_output=True, text=True, timeout=30)
    try:
        j = json.loads(r.stdout)
        return f"已登录（{j.get('authMethod')}）" if j.get("loggedIn") else "未登录：运行 scripts/claude_login.sh"
    except json.JSONDecodeError:
        return f"查不到：{r.stdout[:100]}{r.stderr[:100]}"


def check() -> int:
    from .dingtalk import DingTalk
    ok = True
    try:
        cfg = load_config()
        print(f"配置：{CONFIG_PATH}，允许的钉钉 ID {len(cfg.allowed_user_ids)} 个，短名 {', '.join(cfg.folders)}")
    except Exception as e:  # noqa: BLE001
        print(f"配置：读不到 {CONFIG_PATH}：{e}")
        return 1
    try:
        secret = read_secret(cfg.client_id)
        print("钥匙串：有钉钉密钥")
        asyncio.run(DingTalk(cfg.client_id, secret).check())
        print("钉钉：能拿到调用凭证")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"钉钉：{e}")
    for name, p in cfg.folders.items():
        if not Path(p).expanduser().is_dir():
            ok = False
            print(f"短名 {name}：目录不存在 {p}")
    print(f"Claude：{_claude_logged_in()}")
    print(f"数据目录：{DATA_DIR}")
    return 0 if ok else 1


def whoami() -> int:
    import dingtalk_stream
    cfg = load_config()
    secret = read_secret(cfg.client_id)

    class Handler(dingtalk_stream.CallbackHandler):
        async def process(self, callback):
            d = callback.data
            print(f"发消息的人：{d.get('senderNick')}　钉钉 ID（senderStaffId）：{d.get('senderStaffId')}　会话类型：{d.get('conversationType')}",
                  flush=True)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(cfg.client_id, secret))
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
    print("已连上钉钉。现在在钉钉里给小E发一句话，这里会打印你的钉钉 ID。按 Ctrl+C 结束。", flush=True)
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        pass
    return 0


def run() -> int:
    from .app import Bridge
    from .dingtalk import DingTalk
    cfg = load_config()
    if not cfg.allowed_user_ids:
        print("配置里还没有 allowed_user_ids，先运行 remote-claude whoami 拿到你的钉钉 ID。", file=sys.stderr)
        return 2
    bridge = Bridge(cfg, DingTalk(cfg.client_id, read_secret(cfg.client_id)))
    asyncio.run(bridge.run())
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="remote-claude", description="小E：钉钉 ↔ Mac 上的 Claude Code")
    ap.add_argument("command", choices=["run", "check", "whoami"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("dingtalk_stream").setLevel(logging.WARNING)
    sys.exit({"run": run, "check": check, "whoami": whoami}[a.command]())
