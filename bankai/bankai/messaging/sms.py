"""Twilio SMS transport for the household group thread.

A Twilio number cannot participate in native group MMS, so the "3-way conversation"
is mirrored: each spouse texts the copilot's number; every inbound message is relayed
to the other spouse, and copilot replies broadcast to both. Only numbers listed in
HOUSEHOLD_PHONES are ever answered.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re

import httpx

from .. import config

log = logging.getLogger("bankai.sms")

_NON_DIGIT = re.compile(r"[^\d+]")


def normalize_phone(raw: str) -> str:
    cleaned = _NON_DIGIT.sub("", raw.strip())
    if cleaned.startswith("+"):
        return cleaned
    if len(cleaned) == 10:
        return "+1" + cleaned
    if len(cleaned) == 11 and cleaned.startswith("1"):
        return "+" + cleaned
    return "+" + cleaned if cleaned else ""


def household_phones() -> dict[str, str]:
    """Parse HOUSEHOLD_PHONES ('Ford:+18025551234,Sam:+18025555678') -> {name: E.164}."""
    out: dict[str, str] = {}
    for part in config.HOUSEHOLD_PHONES.split(","):
        if ":" in part:
            name, number = part.split(":", 1)
            if name.strip() and normalize_phone(number):
                out[name.strip()] = normalize_phone(number)
    return out


def _auth() -> tuple[str, str]:
    """Basic-auth pair for the REST API: API key (SK sid + secret) when provided,
    else Account SID + Auth Token. The URL path always uses the Account SID."""
    if config.TWILIO_API_KEY_SID and config.TWILIO_API_KEY_SECRET:
        return (config.TWILIO_API_KEY_SID, config.TWILIO_API_KEY_SECRET)
    return (config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def configured() -> bool:
    has_send_auth = bool(
        (config.TWILIO_API_KEY_SID and config.TWILIO_API_KEY_SECRET)
        or config.TWILIO_AUTH_TOKEN
    )
    return bool(
        config.TWILIO_ACCOUNT_SID
        and has_send_auth
        and config.TWILIO_FROM_NUMBER
        and household_phones()
    )


def identify_sender(from_number: str) -> str | None:
    normalized = normalize_phone(from_number)
    for name, number in household_phones().items():
        if number == normalized:
            return name
    return None


def send_sms(to: str, body: str) -> bool:
    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json",
            auth=_auth(),
            data={"From": config.TWILIO_FROM_NUMBER, "To": to, "Body": body[:1500]},
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.error("SMS send to %s failed: %s", to, exc)
        return False


def broadcast(body: str, exclude: str | None = None) -> int:
    """Send to every household member (optionally excluding one, by name)."""
    sent = 0
    for name, number in household_phones().items():
        if exclude and name == exclude:
            continue
        if send_sms(number, body):
            sent += 1
    return sent


def validate_signature(url: str, params: dict[str, str], signature: str) -> bool:
    """Twilio request validation: base64(HMAC-SHA1(auth_token, url + sorted k+v))."""
    if not config.TWILIO_AUTH_TOKEN:
        return False
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    digest = hmac.new(
        config.TWILIO_AUTH_TOKEN.encode(), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")
