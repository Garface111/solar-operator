"""Notification delivery: Resend HTTP API if configured, else SMTP, else log-only.
Marks firings delivered so retries don't duplicate emails."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy.orm import Session

from .. import config
from ..models import RuleFiring

log = logging.getLogger("bankai.notify")


def send_email(subject: str, body: str) -> bool:
    recipients = config.NOTIFY_EMAILS
    if not recipients:
        log.info("No NOTIFY_EMAILS configured; notification logged only: %s", subject)
        return False
    if config.RESEND_API_KEY:
        try:
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                json={
                    "from": config.NOTIFY_FROM,
                    "to": recipients,
                    "subject": subject,
                    "text": body,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            log.error("Resend send failed: %s", exc)
            return False
    if config.SMTP_HOST:
        try:
            msg = EmailMessage()
            msg["From"] = config.NOTIFY_FROM
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
                smtp.starttls()
                if config.SMTP_USER:
                    smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
                smtp.send_message(msg)
            return True
        except Exception as exc:
            log.error("SMTP send failed: %s", exc)
            return False
    log.info("No email backend configured; notification logged only: %s\n%s", subject, body)
    return False


def deliver_firings(session: Session, firings: list[RuleFiring]) -> int:
    from ..messaging import sms

    delivered = 0
    sms_on = config.NOTIFY_SMS and sms.configured()
    for firing in firings:
        if firing.delivered:
            continue
        ok = send_email(firing.subject, firing.body)
        if sms_on:
            ok = sms.broadcast(f"{firing.subject}\n{firing.body}"[:1200]) > 0 or ok
        if ok:
            firing.delivered = True
            delivered += 1
    return delivered
