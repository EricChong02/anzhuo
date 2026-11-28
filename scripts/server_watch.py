"""（可选）放在你的外网服务器上每分钟跑一次：看家里电脑上的远窗还连不连得上，连续 3 分钟（可调）连不上就给手机发通知，恢复了再发一条。
部署在 /opt/anzhuo-watch/（venv 里装了 pywebpush）；push.json（设备订阅）由远窗在订阅变化时同步过来（配置 watch_host），
vapid.txt 是网页推送的签名私钥。环境变量 ANZHUO_WATCH_SUB 填你的联系方式（mailto:you@example.com 或网址），VAPID 协议要求有；
ANZHUO_WATCH_PORT 是隧道在服务器上的端口，默认 18765；ANZHUO_WATCH_DOWN_AFTER 是连续几分钟连不上才报，默认 3。
已部署的那份照旧写死也能用；重新部署时在 cron 里带上这两个环境变量。
cron：* * * * * ANZHUO_WATCH_SUB=mailto:you@example.com /opt/anzhuo-watch/venv/bin/python /opt/anzhuo-watch/server_watch.py

2026-11-24 修：以前推送一失败（比如服务器连不上 FCM）整个脚本就崩，状态没写下去，
于是每分钟重新“发现掉线”再推一次，Mac 合盖期间手机被反复叫。现在先记状态再推送，
每台设备单独兜住异常，失败最多试 3 次就停，日志也不再刷堆栈。
"""
import json
import os
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
STATE = Path(os.environ.get("ANZHUO_WATCH_STATE", HERE / "state.json"))
HEALTH = f"http://127.0.0.1:{os.environ.get('ANZHUO_WATCH_PORT', '18765')}/_x/health"
DOWN_AFTER = max(1, int(os.environ.get("ANZHUO_WATCH_DOWN_AFTER", "3")))  # 连续几分钟连不上才报
PUSH_TRIES = 3   # 推送失败最多试几次（每次间隔一分钟），试够就停，免得一直重试
DRY = os.environ.get("ANZHUO_WATCH_DRYRUN") == "1"  # 试运行：只写日志不真发


def log(*a) -> None:
    print(time.strftime("%F %T"), *a, flush=True)


def alive() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=8) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - 连不上就是不在线
        return False


def push(title: str, body: str) -> bool:
    """给每台设备发；有一台成功就算发到了。任何一台出错都不影响其他设备，也不再往外抛。"""
    if DRY:
        log("（试运行）该发：", title)
        return True
    try:
        from py_vapid import Vapid
        from pywebpush import webpush
        key = Vapid.from_raw((HERE / "vapid.txt").read_text().strip().encode())
        subs = json.loads((HERE / "push.json").read_text())
    except Exception as e:  # noqa: BLE001 - 密钥或订阅文件坏了：记一行，别崩
        log("推送准备失败：", type(e).__name__, e)
        return False
    data = json.dumps({"title": title, "body": body, "num": None, "tag": "watch"}, ensure_ascii=False)
    ok = 0
    for sub in subs:
        try:
            webpush(sub, data, vapid_private_key=key,
                    vapid_claims={"sub": os.environ.get("ANZHUO_WATCH_SUB", "mailto:admin@example.com")},
                    ttl=3600, timeout=10)   # 不设超时会卡在连不上的推送服务上，cron 进程越堆越多
            ok += 1
        except Exception as e:  # noqa: BLE001 - 单台失败不影响其他设备
            log("有一台没发出去：", type(e).__name__)
    if ok:
        log(f"已发到 {ok} 台：{title}")
    return ok > 0


def save(st: dict) -> None:
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(st))
    temp.replace(STATE)   # 原子替换：cron 每分钟起一次，别让下一个进程读到半个文件


def main() -> None:
    try:
        st = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        st = {"fails": 0, "alerted": False, "since": 0}
    now = time.time()
    if alive():
        was = st.get("alerted")
        mins = int((now - st.get("since", now)) / 60) if was else 0
        save({"fails": 0, "alerted": False, "since": 0, "tries": 0})  # 先记状态，推送出错也不会反复说“恢复了”
        if was:
            try:
                push("远窗恢复了", f"Mac 又连上了（断了约 {mins} 分钟）")
            except Exception as e:  # noqa: BLE001 - 保底：恢复消息发不出去也别让脚本崩
                log("恢复消息发不出去：", type(e).__name__)
        return

    st["fails"] = st.get("fails", 0) + 1
    if st["fails"] == 1:
        st["since"] = now
    if st["fails"] >= DOWN_AFTER and not st.get("alerted"):
        tries = st.get("tries", 0) + 1
        st["tries"] = tries
        save(st)   # 先记再推：推失败下一分钟最多再试到 PUSH_TRIES 次
        try:
            ok = push("远窗连不上了", "Mac 可能睡着、断网或关机了，派的活暂时收不到")
        except Exception as e:  # noqa: BLE001 - 保底：推送自己出错也按“没发到”处理
            log("推送出错：", type(e).__name__)
            ok = False
        if ok:
            st["alerted"] = True
            st["tries"] = 0
            save(st)
        elif tries >= PUSH_TRIES:
            log(f"连续 {tries} 次发不出去，先不重试了，等它恢复")
            st["alerted"] = True
            st["tries"] = 0
            save(st)
        return
    save(st)


if __name__ == "__main__":
    main()
