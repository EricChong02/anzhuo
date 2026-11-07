"""手机通知（Web Push）：远窗加到 iPhone/iPad 主屏幕后，对话做完、等你回答、出错时直接弹系统通知。
签名用的 VAPID 私钥存在钥匙串 anzhuo.webpush（第一次用时生成）；各台设备的订阅存在 push.json。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

KEYCHAIN = "anzhuo.webpush"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _load_vapid():
    """从钥匙串取私钥；没有就生成一把存进去。"""
    from py_vapid import Vapid
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN, "-a", "vapid", "-w"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return Vapid.from_raw(r.stdout.strip().encode())
    v = Vapid()
    v.generate_keys()
    raw = _b64(v.private_key.private_numbers().private_value.to_bytes(32, "big"))
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN, "-a", "vapid", "-w", raw], check=True,
                   capture_output=True)
    return v


class Push:
    def __init__(self, path: Path, contact: str, mirror: str = ""):
        self.path = path
        self.mirror = mirror  # 外网服务器的 ssh 别名：订阅变了同步一份过去，那边的掉线提醒才发得到
        self.contact = contact  # 推送服务要求留个联系方式，用网页地址
        self._vapid = None
        try:
            self.subs: list[dict] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            self.subs = []

    @property
    def vapid(self):
        if self._vapid is None:
            self._vapid = _load_vapid()
        return self._vapid

    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization
        raw = self.vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return _b64(raw)

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.subs, ensure_ascii=False))
        if self.mirror:
            subprocess.Popen(["scp", "-q", "-o", "ConnectTimeout=10", str(self.path), f"{self.mirror}:/opt/anzhuo-watch/push.json"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def add(self, sub: dict) -> None:
        if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://") or not sub.get("keys"):
            raise ValueError("订阅不对")
        self.subs = [s for s in self.subs if s["endpoint"] != sub["endpoint"]] + [
            {"endpoint": sub["endpoint"], "keys": sub["keys"]}]
        self._save()

    def remove(self, endpoint: str) -> None:
        self.subs = [s for s in self.subs if s["endpoint"] != endpoint]
        self._save()

    async def send(self, title: str, body: str, num: int | None = None, only: set[str] | None = None) -> int:
        """给每台开了通知的设备发一条（only 给了就只发这几个订阅）；设备已注销（404/410）的顺手删掉。出错只记日志，不影响别的。
        返回发到了几台（0 = 一台都没发到，小E 改用钉钉说一句）。"""
        subs = [x for x in self.subs if only is None or x["endpoint"] in only]
        if not subs:
            return 0
        from pywebpush import WebPushException, webpush
        data = json.dumps({"title": title, "body": body, "num": num, "tag": f"s{num}" if num else ""}, ensure_ascii=False)
        gone, ok = [], []

        def one(sub):
            try:
                webpush(sub, data, vapid_private_key=self.vapid, vapid_claims={"sub": self.contact}, ttl=3600)
                ok.append(1)
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    gone.append(sub["endpoint"])
                else:
                    log.warning("手机通知没发出去：%s", e)
            except Exception as e:  # noqa: BLE001 - 网络不通之类：这台不算发到
                log.warning("手机通知没发出去：%s", type(e).__name__)

        for sub in subs:
            await asyncio.to_thread(one, sub)
        if gone:
            self.subs = [s for s in self.subs if s["endpoint"] not in gone]
            self._save()
        return len(ok)
