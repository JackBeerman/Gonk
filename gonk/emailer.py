"""Send a report by SMTP. Works with Gmail, Outlook/Office 365, SendGrid, Mailgun, etc.

Configuration (environment variables or .env):
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       587 (STARTTLS, default) or 465 (implicit TLS)
    SMTP_USER       login user (often your email address)
    SMTP_PASSWORD   password or app password (Gmail requires an App Password)
    EMAIL_FROM      sender address (defaults to SMTP_USER)
    EMAIL_TO        comma-separated recipients
    SMTP_STARTTLS   set to 0 to disable STARTTLS (only for a trusted local relay)
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from .report import Report


class EmailConfigError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("EMAIL_TO"))


def build_message(rep: Report, sender: str, recipients: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = rep.subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(rep.text)
    msg.add_alternative(rep.html, subtype="html")
    for name, content in rep.attachments.items():
        if isinstance(content, bytes):
            msg.add_attachment(content, maintype="application", subtype="zip", filename=name)
        else:
            msg.add_attachment(content.encode("utf-8"), maintype="text", subtype="csv", filename=name)
    return msg


def send(rep: Report) -> list[str]:
    host = os.environ.get("SMTP_HOST")
    recipients = [r.strip() for r in os.environ.get("EMAIL_TO", "").split(",") if r.strip()]
    if not host or not recipients:
        raise EmailConfigError("Set SMTP_HOST and EMAIL_TO (plus SMTP_USER/SMTP_PASSWORD) to send email.")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("EMAIL_FROM") or user
    if not sender:
        raise EmailConfigError("Set EMAIL_FROM or SMTP_USER.")
    msg = build_message(rep, sender, recipients)
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        if os.environ.get("SMTP_STARTTLS", "1") != "0":   # set 0 only for a trusted local relay
            server.starttls(context=ctx)
    with server:
        if user and password:
            server.login(user, password)
        server.send_message(msg)
    return recipients
