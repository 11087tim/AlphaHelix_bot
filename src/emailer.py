from __future__ import annotations

import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import unescape

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _html_to_text(html: str) -> str:
    """把 HTML 轉成可讀的純文字（alternative 備用格式，給不支援 HTML 的環境與垃圾信過濾器）。"""
    text = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)>", "\n", text)
    text = re.sub(r"(?is)<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                  lambda m: f"{m.group(2).strip()} ({m.group(1)})" if m.group(2).strip() else m.group(1),
                  text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def send_html_email(
    *,
    gmail_address: str,
    gmail_app_password: str,
    to: list[str],
    subject: str,
    html_body: str,
    bcc: list[str] | None = None,
) -> None:
    """寄送 HTML 信（附純文字 alternative）。to 顯示在信頭；bcc 只進 SMTP 信封（收件人彼此看不到）。"""
    bcc = [a for a in (bcc or []) if a not in to]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = ", ".join(to)  # BCC 不寫入信頭
    # 群發信的退訂管道（Gmail 會在信頭顯示「取消訂閱」，請求會寄回寄件信箱，人工從 config 移除）
    msg["List-Unsubscribe"] = f"<mailto:{gmail_address}?subject=unsubscribe>"
    # 純文字部分放前面、HTML 放後面：支援 HTML 的客戶端會優先顯示最後一個部分
    msg.attach(MIMEText(_html_to_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, to + bcc, msg.as_string())

    logger.info("已寄送摘要信：To %s｜BCC %d 位", ", ".join(to), len(bcc))
