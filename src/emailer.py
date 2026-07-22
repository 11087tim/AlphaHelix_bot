from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def send_html_email(
    *,
    gmail_address: str,
    gmail_app_password: str,
    to: list[str],
    subject: str,
    html_body: str,
    bcc: list[str] | None = None,
) -> None:
    """寄送 HTML 信。to 顯示在信頭；bcc 只進 SMTP 信封（收件人彼此看不到）。"""
    bcc = [a for a in (bcc or []) if a not in to]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = ", ".join(to)  # BCC 不寫入信頭
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, to + bcc, msg.as_string())

    logger.info("已寄送摘要信：To %s｜BCC %d 位", ", ".join(to), len(bcc))
