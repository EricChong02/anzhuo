"""钉钉收发：解析机器人收到的消息；以机器人身份给某人发单聊消息、发图、下载你发来的图片和文件。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.dingtalk.com"
OAPI = "https://oapi.dingtalk.com"
CARD_TEMPLATE = "1366a1eb-bc54-4859-ac88-517c56a9acb1.schema"  # 钉钉公开的 Markdown + 按钮卡片模板


@dataclass
class Incoming:
    sender_id: str
    sender_nick: str
    conversation_type: str  # "1" 单聊，"2" 群聊
    kind: str  # text / picture / richText / audio / file / 其他原样
    text: str = ""
    image_codes: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)  # (downloadCode, 文件名)
    quoted_text: str = ""
    local_images: list[str] = field(default_factory=list)  # 网页上传的图片（已存在本机）
    via: str = "dingtalk"  # 从哪来的：dingtalk / web / auto，或扩展的名字


def _quoted(d: dict) -> str:
    """引用回复时钉钉会带上被引用的那条；字段位置不完全固定，两处都看。"""
    for holder in (d.get("text") or {}, d):
        if not isinstance(holder, dict):
            continue
        rep = holder.get("repliedMsg")
        if isinstance(rep, dict):
            c = rep.get("content")
            if isinstance(c, dict):
                return str(c.get("text") or c.get("content") or json.dumps(c, ensure_ascii=False))
            if isinstance(c, str):
                return c
    return ""


def parse_incoming(d: dict) -> Incoming:
    kind = d.get("msgtype", "")
    inc = Incoming(
        sender_id=str(d.get("senderStaffId") or ""),
        sender_nick=str(d.get("senderNick") or ""),
        conversation_type=str(d.get("conversationType") or ""),
        kind=kind,
        quoted_text=_quoted(d),
    )
    content = d.get("content") or {}
    if kind == "text":
        inc.text = (d.get("text") or {}).get("content", "").strip()
    elif kind == "picture":
        code = content.get("downloadCode") or content.get("pictureDownloadCode")
        if code:
            inc.image_codes.append(code)
    elif kind == "richText":
        parts = []
        for item in content.get("richText", []):
            if "text" in item:
                parts.append(item["text"])
            code = item.get("downloadCode") or item.get("pictureDownloadCode")
            if code:
                inc.image_codes.append(code)
        inc.text = "\n".join(p for p in parts if p).strip()
    elif kind == "audio":
        inc.text = str(content.get("recognition") or "").strip()
    elif kind == "file":
        code = content.get("downloadCode")
        if code:
            inc.files.append((code, content.get("fileName") or "附件"))
    return inc


class DingTalk:
    def __init__(self, client_id: str, secret: str):
        self.client_id = client_id
        self.secret = secret
        self._token = ""
        self._token_exp = 0.0

    # ---- 同步部分，经 asyncio.to_thread 调用 ----
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token
        r = requests.post(f"{API}/v1.0/oauth2/accessToken",
                          json={"appKey": self.client_id, "appSecret": self.secret}, timeout=15)
        r.raise_for_status()
        j = r.json()
        self._token = j["accessToken"]
        self._token_exp = time.time() + int(j.get("expireIn", 7200)) - 300
        return self._token

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(f"{API}{path}", json=body, timeout=30,
                          headers={"x-acs-dingtalk-access-token": self._get_token()})
        if r.status_code == 401:
            self._token = ""
        if r.status_code >= 400:
            raise RuntimeError(f"钉钉接口 {path} 返回 {r.status_code}：{r.text[:300]}")
        return r.json() if r.text else {}

    def _send(self, user_id: str, msg_key: str, param: dict) -> None:
        self._post("/v1.0/robot/oToMessages/batchSend", {
            "robotCode": self.client_id, "userIds": [user_id],
            "msgKey": msg_key, "msgParam": json.dumps(param, ensure_ascii=False),
        })

    def _put(self, path: str, body: dict) -> dict:
        r = requests.put(f"{API}{path}", json=body, timeout=30,
                         headers={"x-acs-dingtalk-access-token": self._get_token()})
        if r.status_code == 401:
            self._token = ""
        if r.status_code >= 400:
            raise RuntimeError(f"钉钉接口 {path} 返回 {r.status_code}：{r.text[:300]}")
        return r.json() if r.text else {}

    def _create_card(self, user_id: str, out_id: str, data: dict) -> None:
        """单聊里发一张卡片（钉钉公开的“Markdown + 按钮”模板）；按钮点了经 Stream 回调回来。"""
        self._post("/v1.0/card/instances/createAndDeliver", {
            "cardTemplateId": CARD_TEMPLATE, "outTrackId": out_id, "userIdType": 1,
            "cardData": {"cardParamMap": data}, "callbackType": "STREAM",
            "openSpaceId": f"dtv1.card//IM_ROBOT.{user_id}",
            "imRobotOpenSpaceModel": {"supportForward": False},
            "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT", "robotCode": self.client_id},
        })

    def _update_card(self, out_id: str, data: dict) -> None:
        self._put("/v1.0/card/instances", {"outTrackId": out_id, "cardData": {"cardParamMap": data},
                                           "cardUpdateOptions": {"updateCardDataByKey": True}})

    def _upload_image(self, path: Path) -> str:
        with open(path, "rb") as f:
            r = requests.post(f"{OAPI}/media/upload", params={"access_token": self._get_token()},
                              data={"type": "image"}, files={"media": (path.name, f)}, timeout=60)
        j = r.json()
        if "media_id" not in j:
            raise RuntimeError(f"上传图片失败：{str(j)[:300]}")
        return j["media_id"]

    def _upload_file(self, path: Path) -> str:
        with open(path, "rb") as f:
            r = requests.post(f"{OAPI}/media/upload", params={"access_token": self._get_token()},
                              data={"type": "file"}, files={"media": (path.name, f)}, timeout=120)
        j = r.json()
        if "media_id" not in j:
            raise RuntimeError(f"上传文件失败：{str(j)[:300]}")
        return j["media_id"]

    def _download(self, code: str, dest: Path) -> Path:
        url = self._post("/v1.0/robot/messageFiles/download",
                         {"robotCode": self.client_id, "downloadCode": code})["downloadUrl"]
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    # ---- 对外的异步接口 ----
    async def check(self) -> None:
        await asyncio.to_thread(self._get_token)

    async def send_markdown(self, user_id: str, title: str, text: str) -> None:
        await asyncio.to_thread(self._send, user_id, "sampleMarkdown", {"title": title, "text": text})

    async def send_text(self, user_id: str, text: str) -> None:
        await asyncio.to_thread(self._send, user_id, "sampleText", {"content": text})

    async def send_image(self, user_id: str, path: Path) -> None:
        media_id = await asyncio.to_thread(self._upload_image, path)
        await asyncio.to_thread(self._send, user_id, "sampleImageMsg", {"photoURL": media_id})

    async def send_file(self, user_id: str, path: Path) -> None:
        media_id = await asyncio.to_thread(self._upload_file, path)
        ext = path.suffix.lstrip(".").lower() or "file"
        await asyncio.to_thread(self._send, user_id, "sampleFile", {"mediaId": media_id, "fileName": path.name, "fileType": ext})

    async def download(self, code: str, dest: Path) -> Path:
        return await asyncio.to_thread(self._download, code, dest)

    async def create_card(self, user_id: str, out_id: str, data: dict) -> None:
        await asyncio.to_thread(self._create_card, user_id, out_id, data)

    async def update_card(self, out_id: str, data: dict) -> None:
        await asyncio.to_thread(self._update_card, out_id, data)


class NoDingTalk:
    """钉钉停用（配置 dingtalk = false）：什么都不发，提醒只走网页和手机通知。"""
    secret = ""

    async def check(self) -> None:
        return None

    async def send_markdown(self, user_id: str, title: str, text: str) -> None:
        return None

    async def send_text(self, user_id: str, text: str) -> None:
        return None

    async def send_image(self, user_id: str, path: Path) -> None:
        return None

    async def send_file(self, user_id: str, path: Path) -> None:
        return None

    async def download(self, code: str, dest: Path) -> Path:
        raise RuntimeError("钉钉已停用")

    async def create_card(self, user_id: str, out_id: str, data: dict) -> None:
        return None

    async def update_card(self, out_id: str, data: dict) -> None:
        return None
