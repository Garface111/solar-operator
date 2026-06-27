"""
NEPOOL Operator — Resend delivery webhook (W2-6, June 2026).

The fire-and-forget send model gave the operator zero signal a report actually
landed — the single most direct contradiction of the value prop ("your report
went out clean"). This handler closes that gap: Resend POSTs delivery lifecycle
events here, and we persist per-CLIENT delivery health (last delivered / last
bounced + reason) so the dashboard can render a truthful indicator.

Resend signs webhooks with the Svix scheme. When RESEND_WEBHOOK_SECRET is set we
verify the signature (HMAC-SHA256 over `{id}.{timestamp}.{body}`, base64); when
it's blank (local dev) we accept unsigned posts. No new dependency — the Svix
verification is a few lines of stdlib hmac.

Handled event types:
  email.delivered  → stamp Client.last_delivered_at
  email.bounced    → stamp Client.last_bounced_at + last_bounce_reason
  email.complained → stamp Client.last_bounced_at + "Marked as spam"

Recipients are matched to a Client by contact_email (case-insensitive). The
[copy]/CC fan-out goes to tenant/cc addresses that don't match a Client's
contact_email, so those events are simply ignored here.
"""
from __future__ import annotations

import os
import json
import hmac
import base64
import hashlib
import logging

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select, func

from .db import SessionLocal
from .models import Client, now

logger = logging.getLogger(__name__)

RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")
# True when running on Railway (prod). Used to FAIL CLOSED if the signing secret
# is unset — never accept unsigned events in production.
_ON_RAILWAY = bool(
    os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_SERVICE_ID")
    or os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME")
)

router = APIRouter()


def _verify_signature(secret: str, svix_id: str | None, svix_ts: str | None,
                      svix_sig: str | None, body: bytes) -> bool:
    """Verify a Svix-signed Resend webhook. Returns True iff a signature in the
    `svix-signature` header matches. Never raises."""
    if not (svix_id and svix_ts and svix_sig):
        return False
    try:
        raw = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
        key = base64.b64decode(raw)
        signed = svix_id.encode() + b"." + svix_ts.encode() + b"." + body
        expected = base64.b64encode(
            hmac.new(key, signed, hashlib.sha256).digest()
        ).decode()
        # Header is a space-separated list of "v1,<sig>" tokens.
        for token in svix_sig.split(" "):
            sig = token.split(",", 1)[1] if "," in token else token
            if hmac.compare_digest(sig, expected):
                return True
        return False
    except Exception:  # noqa: BLE001 — a malformed signature is just invalid
        logger.exception("resend webhook: signature verification errored")
        return False


def _bounce_reason(event_type: str, data: dict) -> str | None:
    """Extract a human reason for a bounce/complaint, capped for storage."""
    if event_type == "email.complained":
        return "Marked as spam"
    bounce = data.get("bounce") or {}
    reason = (
        bounce.get("message")
        or bounce.get("reason")
        or bounce.get("subType")
        or bounce.get("type")
        or "Bounced"
    )
    return str(reason)[:200]


def _recipients(data: dict) -> list[str]:
    to = data.get("to")
    if isinstance(to, str):
        to = [to]
    return [r.strip().lower() for r in (to or []) if isinstance(r, str) and r.strip()]


@router.post("/v1/resend/webhook")
async def resend_webhook(
    request: Request,
    svix_id: str | None = Header(default=None),
    svix_timestamp: str | None = Header(default=None),
    svix_signature: str | None = Header(default=None),
):
    """Receive Resend email lifecycle events and update per-client delivery
    health. Verifies the Svix signature when RESEND_WEBHOOK_SECRET is set."""
    body = await request.body()

    if RESEND_WEBHOOK_SECRET:
        if not _verify_signature(
            RESEND_WEBHOOK_SECRET, svix_id, svix_timestamp, svix_signature, body
        ):
            raise HTTPException(400, "Invalid signature")
    elif _ON_RAILWAY:
        # Fail CLOSED in prod: never process unsigned events that mutate client
        # delivery state. Locally (no secret) we still accept for testing.
        raise HTTPException(503, "Resend webhook not configured")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    event_type = payload.get("type", "")
    data = payload.get("data") or {}
    recipients = _recipients(data)

    if event_type not in ("email.delivered", "email.bounced", "email.complained"):
        return {"ok": True, "ignored": event_type or "unknown"}
    if not recipients:
        return {"ok": True, "matched": 0, "note": "no recipients in payload"}

    reason = _bounce_reason(event_type, data) if event_type != "email.delivered" else None
    ts = now()
    matched: list[int] = []
    with SessionLocal() as db:
        for email in recipients:
            clients = db.execute(
                select(Client).where(func.lower(Client.contact_email) == email)
            ).scalars().all()
            for c in clients:
                if event_type == "email.delivered":
                    c.last_delivered_at = ts
                else:
                    c.last_bounced_at = ts
                    c.last_bounce_reason = reason
                matched.append(c.id)
        db.commit()

    return {"ok": True, "event": event_type, "matched": matched}
