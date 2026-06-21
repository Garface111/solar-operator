"""
NEPOOL Operator — Stripe webhook (extracted from signup.py at v1.1.0).

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
    """LEGACY PATH (pre no-upfront-payment). Card collection was removed from
    signup, so new operators never hit Stripe Checkout during onboarding. This
    handler is kept alive ONLY for in-flight legacy sessions — any operator who
    started Stripe Checkout BEFORE this deploy and completed it after. New signups
    are activated directly in api.onboarding._create_trial_tenant with no Stripe
    round-trip, and card collection now happens via setup_intent.succeeded
    (dashboard add-card flow).

    Activates the pending tenant and advances it to the 'extension' stage.
    Crucially does NOT send the welcome email — that is deferred to POST
    /v1/onboarding/complete so the operator only gets it once they finish setup.

    Handles two modes:
      setup mode (deferred billing) — collect card only, set trial_ends_at
      subscription mode (legacy)    — immediate charge, set stripe_subscription_id
    """
    from datetime import timedelta

    mode = sess.get("mode", "subscription")
    stripe_customer_id = sess.get("customer")

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
        t.onboarding_stage = "extension"
        if stripe_customer_id:
            t.stripe_customer_id = stripe_customer_id

        if mode == "setup":
            setup_intent_id = sess.get("setup_intent")
            stripe_payment_method_id = None
            if setup_intent_id:
                try:
                    si = stripe.SetupIntent.retrieve(setup_intent_id)
                    stripe_payment_method_id = si.get("payment_method")
                except Exception:
                    logger.exception("Could not retrieve SetupIntent %s", setup_intent_id)
            if stripe_payment_method_id:
                t.stripe_payment_method_id = stripe_payment_method_id
            t.trial_ends_at = now() + timedelta(days=14)
            t.subscription_status = "trialing"
        else:
            # Legacy subscription mode: immediate charge, subscription already created.
            stripe_subscription_id = sess.get("subscription")
            if stripe_subscription_id:
                t.stripe_subscription_id = stripe_subscription_id
            t.subscription_status = "active"

        # Seed a placeholder client when no clients pre-entered (Path B).
        # Lazy import avoids a circular dep with onboarding.py.
        from .onboarding import ensure_placeholder_client
        ensure_placeholder_client(db, t.id)
        db.commit()
        tid = t.id
        payment_method_id = t.stripe_payment_method_id

    if mode == "setup":
        send_internal_alert(
            "🌞 Onboarding card collected (trial started)",
            f"Tenant {tid} ({sess.get('customer_email')}) entered card. Trial active.\n"
            f"Stripe customer: {stripe_customer_id}\n"
            f"Payment method: {payment_method_id}"
        )
    else:
        send_internal_alert(
            "🌞 Onboarding payment received",
            f"Tenant {tid} ({sess.get('customer_email')}) paid. "
            f"Awaiting extension install + client setup.\n"
            f"Stripe customer: {stripe_customer_id}\n"
            f"Stripe subscription: {sess.get('subscription')}"
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
            tenant_key=t.tenant_key,
            tenant_name=t.company_name or t.name,
            operator_name=t.operator_name or t.company_name or t.name,
            tenant_email=t.contact_email, tenant_plan=t.plan,
        )

    try:
        send_welcome_email(
            to=snapshot["tenant_email"],
            name=(sess.get("metadata") or {}).get("name") or snapshot["operator_name"],
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
        "🌞 New NEPOOL Operator signup",
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
        # Clear the dead subscription id so a later reactivation creates a fresh
        # subscription instead of short-circuiting on `already_active` in
        # create_subscription_for_tenant.
        t.stripe_subscription_id = None
        db.commit()
        tid, email = t.id, t.contact_email
        name = t.operator_name or t.company_name or t.name
        product = t.product

    from datetime import datetime as _dt
    try:
        send_cancellation_email(to=email, name=name, cancel_date=_dt.utcnow(),
                                product=product)
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
        tid, email = t.id, t.contact_email
        name = t.operator_name or t.company_name or t.name
        product = t.product

    try:
        send_payment_failed_email(
            to=email, name=name,
            amount_dollars=amount_due_cents / 100,
            next_attempt_unix=next_attempt,
            product=product,
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


def _process_payment_method_detached(pm: dict) -> dict:
    """Clear our cached PM when an operator removes their card in Stripe.

    Without this handler, a trialing tenant who detaches their card via the
    billing portal would only be discovered at trial-end when
    finalize_expired_trials() tries to create a subscription with a null PM.
    """
    pm_id = pm.get("id")
    if not pm_id:
        return {"ignored": "no pm id in event"}

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.stripe_payment_method_id == pm_id)
        ).scalars().first()
        if not t:
            return {"ignored": f"no tenant with stripe_payment_method_id={pm_id}"}

        t.stripe_payment_method_id = None
        db.commit()
        tid, email = t.id, t.contact_email

    logger.warning(
        "payment_method.detached: cleared pm_id=%s for tenant=%s", pm_id, tid
    )
    send_internal_alert(
        f"⚠️ Payment method detached mid-trial: {tid}",
        f"Tenant {tid} ({email}) had payment method {pm_id} detached.\n"
        f"If they are still trialing, the trial-end charge will fail.\n"
        f"Consider reaching out to prompt them to re-add a card."
    )
    return {"tenant": tid, "pm_cleared": True}


def _process_setup_intent_succeeded(si: dict) -> dict:
    """Dashboard add-card flow completed: store the card and (if the tenant is
    paused for lack of one) auto-resume their subscription.

    The dashboard's POST /v1/account/add-payment-method creates a Stripe Checkout
    Session in mode='setup' carrying metadata.tenant_id. When the operator
    finishes it, Stripe fires setup_intent.succeeded. We look the tenant up by
    that metadata, store stripe_customer_id + stripe_payment_method_id, and — if
    they were 'paused_no_card' — create the subscription so reports resume with
    no further clicks. Idempotent: re-delivery just re-stores the same IDs and
    create_subscription_for_tenant no-ops once a subscription exists.
    """
    meta = si.get("metadata") or {}
    tenant_id = meta.get("tenant_id")
    if not tenant_id:
        return {"ignored": "no tenant_id in setup_intent metadata"}

    customer_id = si.get("customer")
    payment_method_id = si.get("payment_method")
    if not payment_method_id:
        return {"ignored": f"no payment_method on setup_intent for tenant={tenant_id}"}

    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            return {"ignored": f"no tenant {tenant_id} for setup_intent"}
        t.stripe_payment_method_id = payment_method_id
        if customer_id:
            t.stripe_customer_id = customer_id
        was_paused = t.subscription_status == "paused_no_card"
        # A cancelled tenant adding a card = reactivation. Detect both spellings
        # plus the explicit reactivate=1 metadata flag set by /v1/account/reactivate.
        _status = (t.subscription_status or "").lower()
        was_cancelled = (not t.active) and _status in ("cancelled", "canceled")
        reactivate_flag = str(meta.get("reactivate", "")) == "1"
        db.commit()
        tid, email = t.id, t.contact_email

    logger.info("setup_intent.succeeded: stored pm=%s for tenant=%s (was_paused=%s was_cancelled=%s reactivate=%s)",
                payment_method_id, tid, was_paused, was_cancelled, reactivate_flag)

    result: dict = {"tenant": tid, "pm_stored": True}
    if was_paused or was_cancelled or reactivate_flag:
        # Auto-(re)subscribe with NO trial so the operator doesn't have to click
        # anything else. create_subscription_for_tenant always creates a paid,
        # no-trial subscription (it clears trial_ends_at) and flips active=True.
        from .stripe_helpers import create_subscription_for_tenant
        resume = create_subscription_for_tenant(tid)
        result["resumed"] = bool(resume.get("ok"))
        result["reactivated"] = bool(resume.get("ok")) and (was_cancelled or reactivate_flag)
    else:
        send_internal_alert(
            "💳 Card added",
            f"Tenant {tid} ({email}) added a payment method ({payment_method_id}). "
            f"Not paused — no resume needed."
        )
    return result


# ─── webhook route ─────────────────────────────────────────────────────────

@router.post("/v1/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None)):
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
        except stripe.error.SignatureVerificationError as e:
            logger.warning("webhook: signature verification failed: %s", e)
            raise HTTPException(400, "Invalid signature")
        except Exception as e:
            logger.exception("webhook: construct_event raised unexpected exception")
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
        "setup_intent.succeeded": _process_setup_intent_succeeded,
        "customer.subscription.updated": _process_subscription_updated,
        "customer.subscription.deleted": _process_subscription_deleted,
        "invoice.payment_failed": _process_invoice_payment_failed,
        "payment_method.detached": _process_payment_method_detached,
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
        raw_obj = event["data"]["object"]
        # Stripe SDK v15 removed .get() from StripeObject. Convert to a plain
        # dict so all handler functions can safely use dict.get().
        data_obj = raw_obj.to_dict() if hasattr(raw_obj, "to_dict") else dict(raw_obj)
        result = handler(data_obj)
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
