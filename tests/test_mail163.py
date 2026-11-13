"""mail163 的纯解析逻辑：编码、文件名、正文提取。不连网络。"""
import sys
from email import policy
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mail163


def test_imap_utf7_roundtrip():
    for name in ["INBOX", "已发送", "存档", "重要 & 存档", "a b"]:
        assert mail163.imap_utf7_decode(mail163.imap_utf7_encode(name)) == name
    assert mail163.imap_utf7_encode("已发送") == "&XfJT0ZAB-"


def test_safe_filename_blocks_paths():
    assert mail163.safe_filename("../../etc/passwd") == "passwd"
    assert mail163.safe_filename("..") == "附件"
    assert mail163.safe_filename("合同 2026.pdf") == "合同 2026.pdf"


def test_body_and_attachments():
    msg = EmailMessage()
    msg["From"] = "张三 <zs@example.com>"
    msg["Subject"] = "关于合同 & 发票"
    msg.set_content("第一段\n\n第二段 中文")
    msg.add_alternative("<html><body><p>HTML 正文</p></body></html>", subtype="html")
    msg.add_attachment(b"PDFDATA", maintype="application", subtype="pdf", filename="合同 2026.pdf")
    parsed = __import__("email").message_from_bytes(msg.as_bytes(), policy=policy.default)

    assert str(parsed["Subject"]) == "关于合同 & 发票"
    assert mail163.short_addr(parsed, "From") == "张三 <zs@example.com>"
    text, kind = mail163.body_text(parsed)
    assert "第二段 中文" in text and kind == "text/plain"
    assert [(mail163.safe_filename(p.get_filename()), p.get_content())
            for p in parsed.walk() if p.get_filename()] == [("合同 2026.pdf", b"PDFDATA")]


def test_html_to_text_keeps_links_and_drops_script():
    text = mail163.html_to_text('<p>甲</p><a href="https://example.com/x">乙</a><script>坏</script>')
    assert "甲" in text and "[乙]" in text and "坏" not in text
