"""等所有对话都空闲了再重启远窗。

用远窗来改远窗自己的代码时，跑着的那个对话就在远窗里面，直接重启会把它自己打断（也会打断别的对话）。
所以改完后后台跑本脚本：每 5 秒看一次对话登记，连续 90 秒没有“在做/排队”（留出发报告的时间），
再请 launchd 重启远窗；开了钉钉的话，重启好后在钉钉里告诉你一声。

用法（在仓库根）：.venv/bin/python scripts/restart_when_idle.py [--rename 编号 新名字] &
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from anzhuo.config import DATA_DIR, load_config, read_secret
from anzhuo.dingtalk import DingTalk

LABEL = "local.anzhuo"
QUIET = 90


def busy(path: Path) -> bool:
    try:
        sessions = json.loads(path.read_text()).get("sessions", {})
    except (OSError, json.JSONDecodeError):
        return True
    return any(s.get("status") in ("running", "queued") for s in sessions.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rename", nargs=2, metavar=("NUM", "NAME"), help="重启时顺便给某个对话改名")
    ap.add_argument("--current", help="重启时顺便把某个对话设为当前对话")
    ap.add_argument("--add", nargs=3, metavar=("CLAUDE_ID", "CWD", "NAME"), help="重启时顺便把一个桌面端对话登记进来并设为当前对话")
    a = ap.parse_args()
    path = DATA_DIR / "sessions.json"
    quiet_since = None
    while True:
        if busy(path):
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since >= QUIET:
            break
        time.sleep(5)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    time.sleep(3)
    if a.rename:
        d = json.loads(path.read_text())
        if a.rename[0] in d["sessions"]:
            d["sessions"][a.rename[0]]["name"] = a.rename[1]
            path.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    if a.add:
        d = json.loads(path.read_text())
        if not any(s.get("claude_id") == a.add[0] for s in d["sessions"].values()):
            num = d.get("next_num", 1)
            d["sessions"][str(num)] = {"num": num, "name": a.add[2], "cwd": a.add[1], "claude_id": a.add[0],
                                       "from_desktop": True, "updated": time.time(), "created": time.time()}
            d["next_num"], d["current"] = num + 1, num
            path.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    if a.current:
        d = json.loads(path.read_text())
        if a.current in d["sessions"]:
            d["current"] = int(a.current)
            path.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    subprocess.run([str(Path(__file__).parent / "install_launchd.sh")], capture_output=True)
    time.sleep(15)
    cfg = load_config()
    if cfg.dingtalk_enabled and cfg.allowed_user_ids:
        asyncio.run(DingTalk(cfg.client_id, read_secret(cfg.client_id)).send_markdown(
            cfg.allowed_user_ids[0], "远窗已更新", "远窗已换上新版本。"))


if __name__ == "__main__":
    main()
