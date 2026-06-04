"""
Solar Operator — Stripe webhook (extracted from signup.py at v1.1.0).

The single-shot `POST /v1/signup` flow was retired in favor of the 5-screen
onboarding wizard (`api/onboarding.py`). The Stripe *webhook*, however, is shared
infrastructure that must stay live for BOTH:

  - the new onboarding flow — `checkout.session.completed` sessions carry an
    `onboarding_token` in metadata (added in Task 3). We look the tenant up by
    that token, activate it, and advance `onboarding_stage` to 'extension'
    WITHOUT sending the welcome email (deferred to /v1/onboarding/complete).
  - any in-flight legacy `/v1/signup` checkouts — sessions tagged with a bare
    `tenant_id` (no token) still activate + send the welcome email immediately.
  - subscription lifecycle for ALL existing tenants (updates, cancellations,
    failed invoices) regardless of how they were originally signed up.

So this module was lifted wholesale out of the (now unmounted) signup router and
mounted on its own. The legacy `/v1/signup` and `/v1/checkout/{sid}` endpoints
stayed behind in `api/_legacy_signup.py` and are no longer mounted.
"""
from __future__ import annotations

import os
import logging

import stripe
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, StripeEvent, now
from .notify import (
    send_welcome_email,
    send_internal_alert,
    send_payment_failed_email,
    send_cancellation_email,
)

logger = logging.getLogger(__name__)


# ─── config ──────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter()


# ─── event handlers ───────────────────────────────────────────────────────

def _process_onboarding_checkout_completed(sess: dict, onboarding_token: str) -> dict:
    """New 5-screen onboarding flow: activate the pending tenant and advance
    it to the 'extension' stage. Crucially does NOT send the welcome email —
    that is deferred to POST /v1/onboarding/complete so the operator only gets
    it once they actually finish setup."""
    stripe_customer_id = sess.get("customer")
    stripe_subscription_id = sess.get("subscription")

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.onboarding_token == onboarding_token)
        ).scalar_one_or_none()
        if not t:
            send_internal_alert(
                "Stripe success for unknown onboarding token",
                f"checkout.session.completed fired with onboarding_token="
                f"{onboarding_token} but no such tenant. "
                f"Email on session: {sess.get('customer_email')}"
            )
            return {"ignored": "onboarding tenant not found"}

        t.active = True
        t.subscription_status = "active"
        t.onboarding_stage = "extension"
        if stripe_customer_id:
            t.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id:
            t.stripe_subscription_id = stripe_subscription_id
        db.commit()
        tid = t.id

    send_internal_alert(
        "🌞 Onboarding payment received",
        f"Tenant {tid} ({sess.get('customer_email')}) paid. "
        f"Awaiting extension install + client setup.\n"
        f"Stripe customer: {stripe_customer_id}\n"
        f"Stripe subscription: {stripe_subscription_id}"
    )
    return {"tenant_activated": tid}


def _process_checkout_completed(sess: dict) -> dict:
    """Activate tenant, link Stripe IDs, send welcome email."""
    meta = sess.get("metadata") or {}
    # New onboarding flow tags sessions with onboarding_token — route those to
    # the deferred-welcome handler. Legacy /v1/signup sessions fall through.
    onboarding_token = meta.get("onboarding_token")
    if onboarding_token:
        return _process_onboarding_checkout_completed(sess, onboarding_token)

    tenant_id = meta.get("tenant_id")
    if not tenant_id:
        return {"ignored": "no tenant_id in metadata"}

    stripe_customer_id = sess.get("customer")
    stripe_subscription_id = sess.get("subscription")

    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            send_internal_alert(
                "Stripe success for unknown tenant",
                f"Stripe checkout.session.completed fired with metadata "
                f"tenant_id={tenant_id} but no such tenant in DB. "
                f"Email on session: {sess.get('customer_email')}"
            )
            return {"ignored": "tenant not found"}

        t.active = True
        t.subscription_status = "active"
        if stripe_customer_id:
            t.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id:
            t.stripe_subscription_id = stripe_subscription_id
        db.commit()
        snapshot = dict(
            tenant_key=t.tenant_key, tenant_name=t.name,
            tenant_email=t.contact_email, tenant_plan=t.plan,
        )

    try:
        send_welcome_email(
            to=snapshot["tenant_email"],
            name=(sess.get("metadata") or {}).get("name") or snapshot["tenant_name"],
            tenant_key=snapshot["tenant_key"],
            plan=snapshot["tenant_plan"],
        )
    except Exception as e:
        send_internal_alert(
            "Welcome email failed",
            f"Tenant {tenant_id} ({snapshot['tenant_email']}) paid but "
            f"welcome email failed: {e}. Send manually."
        )

    send_internal_alert(
        "🌞 New Solar Operator signup",
        f"Name: {sess.get('metadata',{}).get('name','?')}\n"
        f"Email: {snapshot['tenant_email']}\n"
        f"Company: {sess.get('metadata',{}).get('company','—')}\n"
        f"Tenant ID: {tenant_id}\n"
        f"Stripe customer: {stripe_customer_id}\n"
        f"Stripe subscription: {stripe_subscription_id}"
    )
    return {"tenant_activated": tenant_id}


def _process_subscription_updated(sub: dict) -> dict:
    """Sync Tenant.subscription_status from Stripe (handles upgrades,
    cancellation-at-period-end toggles, etc.)."""
    sub_id = sub.get("id")
    customer_id = sub.get("customer")
    new_status = sub.get("status")  # active, past_due, canceled, unpaid, trialing
    cancel_at_period_end = sub.get("cancel_at_period_end")

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(
                (Tenant.stripe_subscription_id == sub_id) |
                (Tenant.stripe_customer_id == customer_id)
            )
        ).scalars().first()
        if not t:
            return {"ignored": f"no tenant for subscription={sub_id} customer={customer_id}"}

        old_status = t.subscription_status
        t.subscription_status = new_status
        t.stripe_subscription_id = sub_id
        # Active iff Stripe says so AND not pending-cancel-at-period-end
        t.active = new_status in ("active", "trialing")
        db.commit()
        tid = t.id

    if old_status != new_status:
        send_internal_alert(
            f"Subscription {new_status}: {tid}",
            f"Tenant: {tid}\nOld status: {old_status}\nNew status: {new_status}\n"
            f"Cancel at period end: {cancel_at_period_end}"
        )
    return {"tenant": tid, "status": new_status}


def _process_subscription_deleted(sub: dict) -> dict:
    """Hard cancellation — mark tenant inactive."""
    sub_id = sub.get("id")
    customer_id = sub.get("customer")

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(
                (Tenant.stripe_subscription_id == sub_id) |
                (Tenant.stripe_customer_id == customer_id)
            )
        ).scalars().first()
        if not t:
            return {"ignored": f"no tenant for subscription={sub_id}"}

        t.subscription_status = "canceled"
        t.active = False
        db.commit()
        tid, email, name = t.id, t.contact_email, t.name

    try:
        send_cancellation_email(to=email, name=name)
    except Exception as e:
        logger.warning("cancellation email failed: %s", e)

    send_internal_alert(
        f"❌ Subscription canceled: {tid}",
        f"Tenant {tid} ({email}) canceled their subscription. "
        f"They will no longer receive automatic reports."
    )
    return {"tenant_canceled": tid}


def _process_invoice_payment_failed(invoice: dict) -> dict:
    """Notify customer + ops. Don't deactivate yet (Stripe retries up to 4x);
    customer.subscription.updated will flip status to past_due."""
    customer_id = invoice.get("customer")
    customer_email = invoice.get("customer_email")
    amount_due_cents = invoice.get("amount_due") or 0
    next_attempt = invoice.get("next_payment_attempt")

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.stripe_customer_id == customer_id)
        ).scalars().first()
        if not t:
            return {"ignored": f"no tenant for customer={customer_id}"}
        tid, email, name = t.id, t.contact_email, t.name

    try:
        send_payment_failed_email(
            to=email, name=name,
            amount_dollars=amount_due_cents / 100,
            next_attempt_unix=next_attempt,
        )
    except Exception as e:
        logger.warning("payment-failed email failed: %s", e)

    send_internal_alert(
        f"⚠️ Payment failed: {tid}",
        f"Tenant {tid} ({email}) had a payment fail. "
        f"Amount: ${amount_due_cents/100:.2f}. "
        f"Next attempt: {next_attempt}. Stripe will retry; if all retries fail, "
        f"subscription will move to canceled and we'll deactivate them."
    )
    return {"tenant": tid, "payment_failed": True}


# ─── webhook route ─────────────────────────────────────────────────────────

@router.post("/v1/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default=None)):
    """Receives Stripe events. Handles full subscription lifecycle, idempotent
    via stripe_events table — replays are no-ops."""
    payload = await request.body()

    # Verify signature
    if not STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Event.construct_from(
                __import__("json").loads(payload), stripe.api_key
            )
        except Exception as e:
            raise HTTPException(400, f"Invalid payload: {e}")
    else:
        try:
            event = stripe.Webhook.construct_event(
                payload, stripe_signature, STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError:
            raise HTTPException(400, "Invalid signature")
        except Exception as e:
            raise HTTPException(400, f"Invalid event: {e}")

    event_id = event["id"]
    event_type = event["type"]

    # Idempotency: skip if we've already processed this event
    with SessionLocal() as db:
        existing = db.get(StripeEvent, event_id)
        if existing and existing.status == "processed":
            return {"ok": True, "duplicate": True, "event_id": event_id}
        if not existing:
            db.add(StripeEvent(event_id=event_id, event_type=event_type, status="received"))
            db.commit()

    handlers = {
        "checkout.session.completed": _process_checkout_completed,
        "customer.subscription.updated": _process_subscription_updated,
        "customer.subscription.deleted": _process_subscription_deleted,
        "invoice.payment_failed": _process_invoice_payment_failed,
    }
    handler = handlers.get(event_type)

    if not handler:
        with SessionLocal() as db:
            ev = db.get(StripeEvent, event_id)
            if ev:
                ev.status = "ignored"
                ev.note = f"No handler for {event_type}"
                db.commit()
        return {"ok": True, "event": event_type, "handled": False}

    try:
        result = handler(event["data"]["object"])
        with SessionLocal() as db:
            ev = db.get(StripeEvent, event_id)
            if ev:
                ev.status = "processed"
                ev.processed_at = now()
                ev.tenant_id = result.get("tenant") or result.get("tenant_activated") or result.get("tenant_canceled")
                db.commit()
        return {"ok": True, "event": event_type, **result}
    except Exception as e:
        logger.exception("Webhook handler failed for %s", event_type)
        with SessionLocal() as db:
            ev = db.get(StripeEvent, event_id)
            if ev:
                ev.status = "error"
                ev.note = f"{type(e).__name__}: {e}"[:500]
                db.commit()
        send_internal_alert(
            f"Webhook handler crashed: {event_type}",
            f"Event: {event_id}\nType: {event_type}\nError: {e}"
        )
        raise HTTPException(500, "Handler error — will be retried by Stripe")
