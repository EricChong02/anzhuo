#!/usr/bin/env python3
"""网易 163 邮箱命令行：读信、搜信、取附件、发信。

只用标准库。授权码从 macOS 钥匙串读：

    security add-generic-password -U -s remote-claude.mail163 -a yi_official@163.com -w

也可以用环境变量 MAIL163_USER / MAIL163_AUTH 临时覆盖。

常用：
    mail163.py check                                   # 自检：连通、登录、收件箱
    mail163.py folders                                 # 文件夹
    mail163.py list --limit 20                         # 最近邮件
    mail163.py list --unread --days 7                  # 最近 7 天未读
    mail163.py search --from someone@x.com --subject 合同 --days 30
    mail163.py show 12345                              # 读正文（UID 来自 list/search）
    mail163.py attachments 12345 --out ~/Downloads/邮件附件
    mail163.py send --to a@b.com --subject 标题 --body 正文 --attach 文件.pdf
    mail163.py mark 12345 --unread                     # 标未读/已读
    mail163.py move 12345 --to 存档                     # 移动（中文文件夹名自动编码）

加 --json 可给程序读。
"""

from __future__ import annotations

import argparse
import base64
import email
import imaplib
import json
import os
import re
import smtplib
import subprocess
import sys
import ssl
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

IMAP_HOST, IMAP_PORT = "imap.163.com", 993
SMTP_HOST, SMTP_PORT = "smtp.163.com", 465
KEYCHAIN_SERVICE = "remote-claude.mail163"
DEFAULT_USER = "yi_official@163.com"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")


# ───────── 凭证 ─────────

def keychain_get(user: str) -> str | None:
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def credentials(args) -> tuple[str, str]:
    user = getattr(args, "user", None) or os.environ.get("MAIL163_USER") or DEFAULT_USER
    auth = os.environ.get("MAIL163_AUTH") or keychain_get(user)
    if not auth:
        sys.exit(
            f"没找到 {user} 的授权码。\n"
            f"1. 电脑浏览器登录 mail.163.com → 设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP（要短信验证）\n"
            f"2. 生成“授权码”（16 位左右，不是邮箱登录密码）\n"
            f"3. 在本机终端运行（粘贴授权码，不回显）：\n"
            f"   security add-generic-password -U -s {KEYCHAIN_SERVICE} -a {user} -w\n")
    return user, auth


# ───────── IMAP 连接 ─────────

def connect(args) -> imaplib.IMAP4_SSL:
    user, auth = credentials(args)
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=30)
    except Exception as e:
        sys.exit(f"连不上 {IMAP_HOST}:{IMAP_PORT}：{e}")
    try:
        m.login(user, auth)
    except imaplib.IMAP4.error as e:
        sys.exit(f"登录失败：{e}\n多半是授权码不对，或没开 IMAP/SMTP。重新生成授权码再试。")
    # 网易要求客户端先发 ID，否则后续 SELECT 会报 Unsafe Login
    try:
        m._simple_command(
            "ID",
            f'("name" "remote-claude" "version" "1.0" "vendor" "remote-claude" '
            f'"support-email" "{user}")')
    except Exception:
        pass
    return m


def select(m: imaplib.IMAP4_SSL, folder: str = "INBOX"):
    name = folder if folder.upper() == "INBOX" else imap_utf7_encode(folder)
    typ, dat = m.select(f'"{name}"', readonly=False)
    if typ != "OK":
        sys.exit(f"打不开文件夹「{folder}」：{dat}")


# ───────── IMAP 的 modified UTF-7（中文文件夹名） ─────────

def imap_utf7_encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if "\x20" <= ch <= "\x7e":
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] != "&":
            out.append(s[i])
            i += 1
            continue
        j = s.find("-", i)
        if j < 0:
            out.append(s[i:])
            break
        chunk = s[i + 1:j]
        if not chunk:
            out.append("&")
        else:
            pad = "=" * (-len(chunk) % 4)
            out.append(base64.b64decode(chunk.replace(",", "/") + pad).decode("utf-16-be"))
        i = j + 1
    return "".join(out)


# ───────── 解析 ─────────

class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.href = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "a":
            self.href = dict(attrs).get("href") or ""
            if self.href.startswith("http"):
                self.parts.append(" [")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        elif tag == "a" and self.href is not None:
            if self.href.startswith("http"):
                self.parts.append("]")
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def fmt_date(v) -> str:
    try:
        dt = parsedate_to_datetime(str(v))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v or "")[:25]


def short_addr(msg, field="From") -> str:
    vals = getaddresses([str(msg.get(field, ""))])
    return ", ".join((f"{n} <{a}>" if n else a) for n, a in vals) or str(msg.get(field, ""))


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def body_text(msg) -> tuple[str, str]:
    """返回 (纯文本, 类型)。优先 text/plain，其次把 HTML 转文字。"""
    plain = html = None
    for part in msg.walk():
        if part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            try:
                plain = part.get_content()
            except Exception:
                plain = None
        elif ctype == "text/html" and html is None:
            try:
                html = part.get_content()
            except Exception:
                html = None
    if plain and plain.strip():
        return plain, "text/plain"
    if html:
        return html_to_text(html), "text/html"
    return "", "-"


def safe_filename(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f]", "", name).strip(". ")
    return name[:180] or "附件"


def parse_uid(s: str) -> str:
    if not re.fullmatch(r"\d+", str(s)):
        sys.exit(f"UID 应该是数字：{s}")
    return str(s)


def fetch_raw(m, uid: str) -> bytes:
    typ, dat = m.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK":
        sys.exit(f"取信失败：{dat}")
    for item in dat:
        if isinstance(item, tuple) and len(item) > 1:
            return item[1]
    sys.exit(f"没取到 UID {uid} 的内容（可能已被删除或移走）")


def fetch_headers(m, uid: str) -> tuple[bytes, int, set[str]]:
    typ, dat = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)] RFC822.SIZE)")
    if typ != "OK":
        return b"", 0, set()
    raw, size, flags = b"", 0, set()
    for item in dat:
        if isinstance(item, tuple):
            meta = item[0] or b""
            raw = item[1] or b""
            m2 = re.search(rb"RFC822\.SIZE (\d+)", meta)
            if m2:
                size = int(m2.group(1))
            m3 = re.search(rb"FLAGS \(([^)]*)\)", meta)
            if m3:
                flags = {f.decode().upper() for f in m3.group(1).split()}
    return raw, size, flags


def search_uids(m, args) -> list[str]:
    crit = []
    days = getattr(args, "days", None)
    if days:
        since = datetime.now() - timedelta(days=int(days))
        crit += ["SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}"]
    if getattr(args, "unread", False):
        crit.append("UNSEEN")
    typ, dat = m.uid("SEARCH", *(crit or ["ALL"]))
    if typ != "OK":
        sys.exit(f"搜索失败：{dat}")
    uids = dat[0].decode().split() if dat and dat[0] else []
    return uids  # 升序，旧的在前


def matches(msg, args) -> bool:
    def has(field, needle):
        return needle.lower() in str(msg.get(field, "")).lower()

    if getattr(args, "from_addr", None) and not has("From", args.from_addr):
        return False
    if getattr(args, "subject", None) and not has("Subject", args.subject):
        return False
    if getattr(args, "to", None) and not has("To", args.to):
        return False
    if getattr(args, "text", None):
        body, _ = body_text(msg)
        if args.text.lower() not in body.lower() and not has("Subject", args.text):
            return False
    return True


# ───────── 命令 ─────────

def cmd_check(args):
    user = args.user or os.environ.get("MAIL163_USER") or DEFAULT_USER
    ok = True
    auth = os.environ.get("MAIL163_AUTH") or keychain_get(user)
    if auth:
        print(f"✓ 钥匙串里有 {user} 的授权码（{len(auth)} 位）")
    else:
        print(f"✗ 钥匙串里没有 {user} 的授权码"); ok = False
        print(f"  运行：security add-generic-password -U -s {KEYCHAIN_SERVICE} -a {user} -w")
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
        print("✓ 连上 imap.163.com:993")
        if auth:
            m.login(user, auth)
            print("✓ IMAP 登录成功")
            m._simple_command("ID", f'("name" "remote-claude" "version" "1.0" "vendor" "remote-claude")')
            typ, dat = m.select("INBOX")
            if typ == "OK":
                print(f"✓ 收件箱可读，共 {int(dat[0])} 封")
            else:
                print(f"✗ 收件箱打不开：{dat}"); ok = False
        m.logout()
    except Exception as e:
        print(f"✗ IMAP：{e}"); ok = False
    if auth:
        try:
            s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
            s.login(user, auth)
            s.quit()
            print("✓ SMTP 登录成功（发信可用）")
        except Exception as e:
            print(f"✗ SMTP：{e}"); ok = False
    print("\n全部通过。" if ok else "\n有没通过的，按上面的提示处理。")
    sys.exit(0 if ok else 1)


def cmd_folders(args):
    m = connect(args)
    typ, dat = m.list()
    rows = []
    for line in dat or []:
        if not line:
            continue
        s = line.decode("utf-8", "replace")
        mt = re.match(r'\(([^)]*)\) "([^"]*)" "?(.*?)"?$', s)
        if not mt:
            continue
        raw = mt.group(3)
        rows.append({"raw": raw, "name": imap_utf7_decode(raw.strip('"')), "flags": mt.group(1)})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"{r['name']}" + ("" if r["name"] == r["raw"] else f"  ({r['raw']})"))
    m.logout()


def cmd_list(args):
    m = connect(args)
    select(m, args.folder)
    uids = search_uids(m, args)[-int(args.limit):]
    rows = []
    for uid in uids:
        raw, size, flags = fetch_headers(m, uid)
        if not raw:
            continue
        msg = email.message_from_bytes(raw, policy=policy.default)
        rows.append({"uid": uid, "date": fmt_date(msg.get("Date")),
                     "from": short_addr(msg, "From"), "subject": str(msg.get("Subject", "") or "(无主题)"),
                     "size": size, "seen": "\\SEEN" in flags})
    rows.reverse()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        if not rows:
            print("没有符合条件的邮件。")
        for r in rows:
            print(f"{r['uid']:>8}  {r['date']}  {r['from'][:34]:<34}  {r['subject'][:46]}  "
                  f"{human_size(r['size'])}  {'已读' if r['seen'] else '未读'}")
    m.logout()


def cmd_search(args):
    m = connect(args)
    select(m, args.folder)
    uids = search_uids(m, args)
    rows = []
    for uid in reversed(uids):
        if len(rows) >= int(args.limit):
            break
        raw, size, flags = fetch_headers(m, uid)
        if not raw:
            continue
        msg = email.message_from_bytes(raw, policy=policy.default)
        if getattr(args, "text", None):
            # 正文搜索要整封，header 里没有内容
            msg = email.message_from_bytes(fetch_raw(m, uid), policy=policy.default)
        if not matches(msg, args):
            continue
        rows.append({"uid": uid, "date": fmt_date(msg.get("Date")),
                     "from": short_addr(msg, "From"), "subject": str(msg.get("Subject", "") or "(无主题)"),
                     "size": size, "seen": "\\SEEN" in flags})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        if not rows:
            print("没有符合条件的邮件。")
        for r in rows:
            print(f"{r['uid']:>8}  {r['date']}  {r['from'][:34]:<34}  {r['subject'][:46]}  "
                  f"{human_size(r['size'])}  {'已读' if r['seen'] else '未读'}")
    m.logout()


def cmd_show(args):
    m = connect(args)
    select(m, args.folder)
    uid = parse_uid(args.uid)
    msg = email.message_from_bytes(fetch_raw(m, uid), policy=policy.default)
    text, kind = body_text(msg)
    atts = [(safe_filename(p.get_filename()),
             len(p.get_content() or b"") if not p.is_multipart() else 0)
            for p in msg.walk() if p.get_filename()]
    if args.json:
        print(json.dumps({
            "uid": uid, "date": fmt_date(msg.get("Date")),
            "from": short_addr(msg, "From"), "to": short_addr(msg, "To"),
            "subject": str(msg.get("Subject", "") or ""),
            "body": text[: int(args.max_chars)], "content_type": kind,
            "attachments": [{"name": n, "size": s} for n, s in atts],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"UID      {uid}")
        print(f"日期     {fmt_date(msg.get('Date'))}")
        print(f"发件人   {short_addr(msg, 'From')}")
        print(f"收件人   {short_addr(msg, 'To')}")
        if msg.get("Cc"):
            print(f"抄送     {short_addr(msg, 'Cc')}")
        print(f"主题     {msg.get('Subject', '') or '(无主题)'}")
        if atts:
            print("附件     " + "；".join(f"{n}（{human_size(s)}）" for n, s in atts))
        print("-" * 60)
        print(text[: int(args.max_chars)] if text else "（没有可读正文）")
        if len(text) > int(args.max_chars):
            print(f"\n…正文还有 {len(text) - int(args.max_chars)} 字，用 --max-chars 调大。")
    m.logout()


def cmd_attachments(args):
    m = connect(args)
    select(m, args.folder)
    uid = parse_uid(args.uid)
    msg = email.message_from_bytes(fetch_raw(m, uid), policy=policy.default)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for part in msg.walk():
        name = part.get_filename()
        if not name:
            continue
        data = part.get_content()
        if not isinstance(data, (bytes, bytearray)):
            data = str(data).encode("utf-8", "replace")
        path = out / safe_filename(name)
        n = 2
        while path.exists():
            path = out / f"{path.stem}-{n}{path.suffix}"
            n += 1
        path.write_bytes(bytes(data))
        saved.append({"name": path.name, "path": str(path), "size": len(data)})
    if args.json:
        print(json.dumps(saved, ensure_ascii=False, indent=2))
    else:
        if not saved:
            print("这封信没有附件。")
        for s in saved:
            print(f"{s['path']}  ({human_size(s['size'])})")
    m.logout()


def cmd_save(args):
    m = connect(args)
    select(m, args.folder)
    uid = parse_uid(args.uid)
    raw = fetch_raw(m, uid)
    path = Path(args.out).expanduser()
    if path.is_dir():
        msg = email.message_from_bytes(raw, policy=policy.default)
        path = path / f"{uid}-{safe_filename(str(msg.get('Subject', 'mail')))[:60]}.eml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    print(path)
    m.logout()


def cmd_send(args):
    user, auth = credentials(args)
    msg = EmailMessage()
    msg["From"] = f"{args.from_name} <{user}>" if args.from_name else user
    msg["To"] = ", ".join(args.to)
    if args.cc:
        msg["Cc"] = ", ".join(args.cc)
    msg["Subject"] = args.subject
    body = args.body
    if args.body_file:
        body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
    msg.set_content(body or "")
    if args.html_file:
        msg.add_alternative(Path(args.html_file).expanduser().read_text(encoding="utf-8"), subtype="html")
    for p in args.attach or []:
        path = Path(p).expanduser()
        if not path.is_file():
            sys.exit(f"附件不存在：{p}")
        data = path.read_bytes()
        ctype = None
        if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ctype = "image/" + path.suffix.lower().lstrip(".").replace("jpg", "jpeg")
        msg.add_attachment(data, maintype=(ctype.split("/")[0] if ctype else "application"),
                           subtype=(ctype.split("/")[1] if ctype else "octet-stream"),
                           filename=path.name)
    to_all = args.to + (args.cc or []) + (args.bcc or [])
    try:
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60)
        s.login(user, auth)
        s.send_message(msg, to_addrs=to_all)
        s.quit()
    except Exception as e:
        sys.exit(f"发送失败：{e}")
    print(f"已发送给 {', '.join(args.to)}" + (f"，抄送 {', '.join(args.cc)}" if args.cc else ""))


def cmd_mark(args):
    m = connect(args)
    select(m, args.folder)
    flag = "-FLAGS" if args.unread else "+FLAGS"
    for uid in args.uids:
        m.uid("STORE", parse_uid(uid), flag, r"(\Seen)")
    print(f"已标记 {len(args.uids)} 封为{'未读' if args.unread else '已读'}。")
    m.logout()


def cmd_move(args):
    m = connect(args)
    select(m, args.folder)
    target = args.to if args.to.upper() == "INBOX" else imap_utf7_encode(args.to)
    for uid in args.uids:
        uid = parse_uid(uid)
        typ, dat = m.uid("COPY", uid, f'"{target}"')
        if typ != "OK":
            sys.exit(f"移动 UID {uid} 失败：{dat}")
        m.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    m.expunge()
    print(f"已把 {len(args.uids)} 封移到「{args.to}」。")
    m.logout()


def cmd_delete(args):
    m = connect(args)
    select(m, args.folder)
    for uid in args.uids:
        m.uid("STORE", parse_uid(uid), "+FLAGS", r"(\Deleted)")
    m.expunge()
    print(f"已删除 {len(args.uids)} 封。")
    m.logout()


def main():
    ap = argparse.ArgumentParser(description="网易 163 邮箱：读信、搜信、取附件、发信")
    ap.add_argument("--user", help=f"邮箱地址（默认 {DEFAULT_USER}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="自检连通与登录")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("folders", help="列出文件夹")
    p.set_defaults(func=cmd_folders)

    for name, fn, helptext in (("list", cmd_list, "列出最近邮件"), ("search", cmd_search, "搜索邮件")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--folder", default="INBOX")
        p.add_argument("--limit", default="20")
        p.add_argument("--days", type=int, help="只看最近这些天")
        p.add_argument("--unread", action="store_true", help="只看未读")
        if name == "search":
            p.add_argument("--from-addr", dest="from_addr", help="发件人包含")
            p.add_argument("--subject", help="主题包含")
            p.add_argument("--to", help="收件人包含")
            p.add_argument("--text", help="正文或主题包含")
        p.set_defaults(func=fn)

    p = sub.add_parser("show", help="显示一封邮件")
    p.add_argument("uid")
    p.add_argument("--folder", default="INBOX")
    p.add_argument("--max-chars", default="8000")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("attachments", help="下载附件")
    p.add_argument("uid")
    p.add_argument("--folder", default="INBOX")
    p.add_argument("--out", default=str(Path.home() / "Downloads" / "邮件附件"))
    p.set_defaults(func=cmd_attachments)

    p = sub.add_parser("save", help="保存整封 .eml")
    p.add_argument("uid")
    p.add_argument("--folder", default="INBOX")
    p.add_argument("--out", default=str(Path.home() / "Downloads" / "邮件附件"))
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("send", help="发信")
    p.add_argument("--to", action="append", required=True)
    p.add_argument("--cc", action="append")
    p.add_argument("--bcc", action="append")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--body-file")
    p.add_argument("--html-file")
    p.add_argument("--attach", action="append")
    p.add_argument("--from-name", default="庄毅")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("mark", help="标记已读/未读")
    p.add_argument("uids", nargs="+")
    p.add_argument("--folder", default="INBOX")
    p.add_argument("--unread", action="store_true")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("move", help="移动到文件夹")
    p.add_argument("uids", nargs="+")
    p.add_argument("--from-folder", dest="folder", default="INBOX")
    p.add_argument("--to", required=True)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("delete", help="删除（移入已删除）")
    p.add_argument("uids", nargs="+")
    p.add_argument("--folder", default="INBOX")
    p.set_defaults(func=cmd_delete)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
