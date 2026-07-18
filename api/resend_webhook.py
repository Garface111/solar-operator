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
  email.delivered  → stamp Client.last_delivered_at (+ offtaker BillingReportSubscription)
  email.bounced    → stamp Client.last_bounced_at + last_bounce_reason (+ offtaker sub)
  email.complained → stamp Client.last_bounced_at + "Marked as spam" (+ offtaker sub)

Recipients are matched to a Client by contact_email (case-insensitive) and to
BillingReportSubscription by client_email (case-insensitive). When the payload
carries an email_id that matches sub.last_resend_email_id, that offtaker row is
preferred for a precise stamp. The [copy COPCC fan-out goes to tenant/cc
addresses that don't match a Client's contact_email, so those events are simply
ignored for Client matching (receipts are still recorded).
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
from .models import Client, BillingReportSubscription, now

logger = logging.getLogger(__name__)

RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
# True when running on Railway (prod). Used to FAIL CLOSED if the signing secret
# is unset — never accept unsigned events in production.
_ON_RAILWAY = bool(
    os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_SERVICE_ID")
    or os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME")
)

router = APIRouter()


def _headers_get(data: dict, *names) -> str | None:
    """Pull a header value from a Resend received-email object, whether headers
    are a dict, a list of {name,value}, or a top-level field."""
    wanted = {n.lower() for n in names}
    for n in names:
        v = data.get(n) or data.get(n.replace("-", "_"))
        if v:
            return str(v)
    h = data.get("headers")
    if isinstance(h, dict):
        for k, v in h.items():
            if str(k).lower() in wanted and v:
                return str(v)
    elif isinstance(h, list):
        for item in h:
            if isinstance(item, dict) and str(item.get("name", "")).lower() in wanted:
                return str(item.get("value") or "") or None
    return None


def _fetch_received_email(email_id: str) -> tuple[str | None, str | None]:
    """Pull (plain-text body, Message-ID) for an inbound message by id."""
    if not email_id or not RESEND_API_KEY:
        return None, None
    try:
        import urllib.request
        import json as _json
        import re as _re

        req = urllib.request.Request(
            f"https://api.resend.com/emails/receiving/{email_id}",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "User-Agent": "solar-operator-inbound/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        msgid = _headers_get(data, "message-id", "message_id", "Message-ID")
        text = data.get("text") or data.get("text_body") or data.get("body") or ""
        if text:
            return str(text), msgid
        html = data.get("html") or data.get("html_body") or ""
        if html:
            text = _re.sub(r"<[^>]+>", " ", str(html))
            text = _re.sub(r"\s+", " ", text).strip()
            return (text or None), msgid
        return None, msgid
    except Exception:
        logger.exception("resend: failed to fetch received email %s", email_id)
    return None, None


def _fetch_received_email_text(email_id: str) -> str | None:
    return _fetch_received_email(email_id)[0]


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

    # ── Inbound tech replies on repair tickets (Resend inbound / email.received) ──
    # Resend does NOT put body in the webhook — only metadata + email_id.
    # Fetch content via Received Emails API: GET /emails/receiving/{email_id}
    if event_type in ("email.received", "email.inbound", "inbound"):
        from_email = None
        fr = data.get("from") or data.get("sender")
        if isinstance(fr, str):
            from_email = fr
        elif isinstance(fr, dict):
            from_email = fr.get("email") or fr.get("address") or fr.get("value")
        # "Name <addr@x.com>" → addr@x.com
        if from_email and "<" in from_email and ">" in from_email:
            try:
                from_email = from_email.split("<", 1)[1].split(">", 1)[0].strip()
            except Exception:
                pass
        subject = data.get("subject") or ""
        body = (
            data.get("text")
            or data.get("body")
            or data.get("text_body")
            or ""
        )
        email_id = data.get("email_id") or data.get("id")
        # The tech's Message-ID — so our reply threads under it in Gmail.
        inbound_msgid = _headers_get(data, "message-id", "message_id", "Message-ID")
        if (not body or not inbound_msgid) and email_id:
            _t, _mid = _fetch_received_email(email_id)
            body = body or (_t or "")
            inbound_msgid = inbound_msgid or _mid
        if not body and data.get("html"):
            import re as _re
            body = _re.sub(r"<[^>]+>", " ", str(data.get("html")))
            body = _re.sub(r"\s+", " ", body).strip()
        # Prefer received_for (actual inbound address) when present
        to_list = recipients
        rf = data.get("received_for") or data.get("to")
        if isinstance(rf, list) and rf:
            to_list = [str(x).strip().lower() for x in rf if x]
        elif isinstance(rf, str) and rf.strip():
            to_list = [rf.strip().lower()]
        # Sovereign mailbox (Ford ↔ product mind) — check before repair tickets
        try:
            from .energy_agent_sovereign_desk import (
                is_sovereign_inbound_address,
                ingest_sovereign_inbound_async,
            )
            if is_sovereign_inbound_address(to_list):
                # Async: desk LLM can exceed Resend webhook timeout
                ingest_sovereign_inbound_async(
                    from_email=from_email,
                    to_emails=to_list,
                    subject=subject,
                    body=body,
                    resend_email_id=str(email_id) if email_id else None,
                )
                logger.info(
                    "resend inbound sovereign queued: email_id=%s from=%s to=%s",
                    email_id, from_email, to_list,
                )
                return {
                    "ok": True,
                    "event": event_type,
                    "sovereign_inbound": {"queued": True},
                    "email_id": email_id,
                }
        except Exception:
            logger.exception("resend webhook: sovereign inbound route failed")

        # Owner ⇄ Energy Agent mailbox (weekly check-in replies, escalation
        # replies, "hey can you…" emails). Crew mail goes to repairs@ (its own
        # Reply-To), so ANYTHING to the owner agent mailbox is an owner turn —
        # including escalation replies that carry an [AO-TICKET-N] token (the
        # owner ingest routes those to the repair handler internally).
        try:
            from .energy_agent_email import (
                ingest_owner_email_async,
                is_owner_agent_address,
            )
            if is_owner_agent_address(to_list):
                ingest_owner_email_async(
                    from_email=from_email,
                    subject=subject,
                    body=body,
                    resend_email_id=str(email_id) if email_id else None,
                )
                logger.info(
                    "resend inbound owner-agent queued: email_id=%s from=%s",
                    email_id, from_email,
                )
                return {
                    "ok": True,
                    "event": event_type,
                    "owner_agent_inbound": {"queued": True},
                    "email_id": email_id,
                }
        except Exception:
            logger.exception("resend webhook: owner-agent route failed")

        try:
            from . import repair_ops
            with SessionLocal() as db:
                result = repair_ops.ingest_inbound_email(
                    db,
                    from_email=from_email,
                    to_emails=to_list,
                    subject=subject,
                    body=body,
                    resend_email_id=str(email_id) if email_id else None,
                    inbound_message_id=inbound_msgid,
                )
            logger.info(
                "resend inbound repair: email_id=%s matched=%s ticket=%s reason=%s",
                email_id,
                result.get("matched"),
                result.get("ticket_id"),
                result.get("reason"),
            )
            return {
                "ok": True,
                "event": event_type,
                "repair_inbound": result,
                "email_id": email_id,
            }
        except Exception:
            logger.exception("resend webhook: inbound repair parse failed")
            return {"ok": True, "event": event_type, "repair_inbound": {"ok": False}}

    if event_type not in ("email.delivered", "email.bounced", "email.complained"):
        return {"ok": True, "ignored": event_type or "unknown"}
    if not recipients:
        return {"ok": True, "matched": 0, "note": "no recipients in payload"}

    reason = _bounce_reason(event_type, data) if event_type != "email.delivered" else None
    ts = now()
    matched: list[int] = []
    matched_subs: list[int] = []
    recorded = 0
    # Resend puts the sent-message id on data.email_id (and sometimes data.id).
    email_id = data.get("email_id") or data.get("id")
    email_id_str = str(email_id) if email_id else None

    def _stamp_sub(sub: BillingReportSubscription) -> None:
        if event_type == "email.delivered":
            sub.last_delivered_at = ts
        else:
            sub.last_bounced_at = ts
            sub.last_bounce_reason = reason
        if sub.id not in matched_subs:
            matched_subs.append(sub.id)

    with SessionLocal() as db:
        # Prefer precise match by Resend email id when we stamped it on send.
        if email_id_str:
            id_subs = db.execute(
                select(BillingReportSubscription).where(
                    BillingReportSubscription.last_resend_email_id == email_id_str,
                    BillingReportSubscription.deleted_at.is_(None),
                )
            ).scalars().all()
            for sub in id_subs:
                _stamp_sub(sub)

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

            # Offtaker invoices: match BillingReportSubscription.client_email.
            # Still match by email even when id-matched above — covers older
            # sends without last_resend_email_id and multi-sub same-email rows.
            offtaker_subs = db.execute(
                select(BillingReportSubscription).where(
                    func.lower(BillingReportSubscription.client_email) == email,
                    BillingReportSubscription.deleted_at.is_(None),
                )
            ).scalars().all()
            for sub in offtaker_subs:
                _stamp_sub(sub)

            # Keep the receipt for EVERY recipient, not just offtakers. Resend
            # reports on all of them; we used to bin anything that wasn't a
            # Client — including repair techs and the owner — which left the
            # agent unable to answer "did that email land?" (Ford 2026-07-16).
            try:
                from .energy_agent import EaEmailDelivery, _ea_ensure_email_delivery_table
                _ea_ensure_email_delivery_table(db)
                db.add(EaEmailDelivery(
                    to_email=email,
                    event=(event_type or "").replace("email.", "")[:24],
                    subject=(data.get("subject") or None),
                    reason=reason,
                    resend_email_id=email_id_str,
                    created_at=ts,
                ))
                recorded += 1
            except Exception:  # noqa: BLE001 — a receipt must never break the webhook
                logger.exception("resend: failed to record delivery receipt for %s", email)
        db.commit()

    return {
        "ok": True,
        "event": event_type,
        "matched": matched,
        "matched_subs": matched_subs,
        "recorded": recorded,
    }
