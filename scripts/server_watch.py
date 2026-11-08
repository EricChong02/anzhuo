"""（可选）放在你的外网服务器上每分钟跑一次：看家里电脑上的远窗还连不连得上，连续 3 分钟连不上就给手机发通知，恢复了再发一条。
部署在 /opt/anzhuo-watch/（venv 里装了 pywebpush）；push.json（设备订阅）由远窗在订阅变化时同步过来（配置 watch_host），
vapid.txt 是网页推送的签名私钥。环境变量 ANZHUO_WATCH_SUB 填你的联系方式（mailto:you@example.com 或网址），VAPID 协议要求有；
ANZHUO_WATCH_PORT 是隧道在服务器上的端口，默认 18765。
已部署的那份照旧写死也能用；重新部署时在 cron 里带上这两个环境变量。
cron：* * * * * ANZHUO_WATCH_SUB=mailto:you@example.com /opt/anzhuo-watch/venv/bin/python /opt/anzhuo-watch/server_watch.py"""
import json
import os
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / "state.json"
HEALTH = f"http://127.0.0.1:{os.environ.get('ANZHUO_WATCH_PORT', '18765')}/_x/health"
DOWN_AFTER = 3  # 连续几分钟连不上才报，免得远窗正常重启时误报


def alive() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=8) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def push(title: str, body: str) -> None:
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    key = Vapid.from_raw((HERE / "vapid.txt").read_text().strip().encode())
    data = json.dumps({"title": title, "body": body, "num": None, "tag": "watch"}, ensure_ascii=False)
    for sub in json.loads((HERE / "push.json").read_text()):
        try:
            webpush(sub, data, vapid_private_key=key, vapid_claims={"sub": os.environ.get("ANZHUO_WATCH_SUB", "mailto:admin@example.com")}, ttl=3600)
        except WebPushException as e:
            print(time.strftime("%F %T"), "发不出去", e)


def main() -> None:
    try:
        st = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        st = {"fails": 0, "alerted": False, "since": 0}
    if alive():
        if st.get("alerted"):
            mins = int((time.time() - st.get("since", time.time())) / 60)
            push("远窗恢复了", f"Mac 又连上了（断了约 {mins} 分钟）")
        st = {"fails": 0, "alerted": False, "since": 0}
    else:
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] == 1:
            st["since"] = time.time()
        if st["fails"] >= DOWN_AFTER and not st.get("alerted"):
            push("远窗连不上了", "Mac 可能睡着、断网或关机了，派的活暂时收不到")
            st["alerted"] = True
    STATE.write_text(json.dumps(st))


if __name__ == "__main__":
    main()
