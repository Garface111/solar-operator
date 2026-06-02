"""
Solar Operator — signup + Stripe checkout + webhook handlers.

Single-plan model (June 2026 onwards): one product, $75/month, unlimited arrays.
The PLANS dict and ?plan= API parameter are gone — the legacy frontend
sends `plan: "standard"` or omits the field; we accept both for compatibility.

Public flow:
  POST /v1/signup            → creates a "pending" tenant + Stripe Checkout session,
                               returns the checkout URL for the frontend to redirect to.
  POST /v1/stripe/webhook    → Stripe → tenant activation, lifecycle updates,
                               welcome email, idempotent on event.id.
  GET  /v1/checkout/{sid}    → post-payment lookup: shows activation code inline.
"""
from __future__ import annotations

import os
import secrets
import logging
from typing import Optional

import stripe
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, StripeEvent, now
from .notify import send_welcome_email, send_internal_alert, send_payment_failed_email, send_cancellation_email

logger = logging.getLogger(__name__)


# ─── config ──────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")  # price_xxx — set in Stripe dashboard
APP_URL = os.getenv("APP_URL", "https://solaroperator.org").rstrip("/")
API_URL = os.getenv("API_URL", "https://web-production-49c83.up.railway.app").rstrip("/")
PRICE_CENTS = int(os.getenv("PLAN_PRICE_CENTS", "7500"))  # $75/mo fallback
PLAN_NAME = os.getenv("PLAN_NAME", "Solar Operator")
PLAN_DESCRIPTION = os.getenv("PLAN_DESCRIPTION", "Unlimited arrays · Automatic monthly reporting")

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter()


# ─── schemas ─────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    company: Optional[str] = Field(None, max_length=200)
    plan: Optional[str] = Field(default=None, max_length=32)  # legacy: ignored


class SignupResponse(BaseModel):
    checkout_url: str
    tenant_id: str


# ─── helpers ─────────────────────────────────────────────────────────────

def gen_tenant_id() -> str:
    return "ten_" + secrets.token_hex(8)


def gen_tenant_key() -> str:
    return "sol_live_" + secrets.token_urlsafe(24)


def _checkout_line_item() -> dict:
    """If STRIPE_PRICE_ID is set, use it (required for Billing Portal). Otherwise
    fall back to inline price_data for dev mode."""
    if STRIPE_PRICE_ID:
        return {"price": STRIPE_PRICE_ID, "quantity": 1}
    return {
        "price_data": {
            "currency": "usd",
            "product_data": {"name": PLAN_NAME, "description": PLAN_DESCRIPTION},
            "unit_amount": PRICE_CENTS,
            "recurring": {"interval": "month"},
        },
        "quantity": 1,
    }


# ─── public: signup ──────────────────────────────────────────────────────

@router.post("/v1/signup", response_model=SignupResponse)
def signup(req: SignupRequest):
    """Create a pending tenant + Stripe Checkout session.

    Tenant is marked active=False until webhook (or /v1/checkout/{sid}
    self-heal) fires on successful payment.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe is not configured on this server")

    tenant_id = gen_tenant_id()
    tenant_key = gen_tenant_key()
    display_name = (req.company or req.name).strip()[:200]

    with SessionLocal() as db:
        # If email already has an active tenant, refuse (cheap dedupe)
        existing = db.execute(
            select(Tenant).where(Tenant.contact_email == req.email, Tenant.active == True)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409,
                "An account already exists for this email. "
                "Email support@solaroperator.org if you've lost access.")

        t = Tenant(
            id=tenant_id, name=display_name, contact_email=req.email,
            tenant_key=tenant_key, plan="standard", active=False, created_at=now(),
            subscription_status="pending",
        )
        db.add(t)
        db.commit()

    # Build Stripe Checkout session
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer_email=req.email,
            line_items=[_checkout_line_item()],
            success_url=f"{APP_URL}/welcome.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/signup.html?cancelled=1",
            allow_promotion_codes=True,
            metadata={
                "tenant_id": tenant_id,
                "plan": "standard",
                "name": req.name,
                "company": req.company or "",
            },
            subscription_data={
                "metadata": {"tenant_id": tenant_id, "plan": "standard"},
            },
        )
    except stripe.error.StripeError as e:
        # Roll back the pending tenant — no point keeping orphans
        with SessionLocal() as db:
            t = db.get(Tenant, tenant_id)
            if t:
                db.delete(t); db.commit()
        raise HTTPException(502, f"Stripe error: {str(e)}")

    return SignupResponse(checkout_url=session.url, tenant_id=tenant_id)


# ─── stripe webhook ─────────────────────────────────────────────────────

def _process_checkout_completed(sess: dict) -> dict:
    """Activate tenant, link Stripe IDs, send welcome email."""
    tenant_id = (sess.get("metadata") or {}).get("tenant_id")
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
        f"They will no longer receive monthly reports."
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


# ─── public: fetch activation code by Stripe session id ─────────────────

@router.get("/v1/checkout/{session_id}")
def checkout_lookup(session_id: str):
    """Called by the post-payment welcome page to show the activation code
    inline (no email dependency). Verifies the session was actually paid,
    then returns the tenant_key. Self-heals if the webhook lagged or failed
    by activating the tenant + back-filling Stripe IDs on first lookup.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe is not configured on this server")
    if not session_id.startswith("cs_"):
        raise HTTPException(400, "Invalid session id")

    try:
        sess = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe lookup failed: {e}")

    if sess.get("payment_status") not in ("paid", "no_payment_required"):
        raise HTTPException(402, "Payment not complete yet")

    tenant_id = (sess.get("metadata") or {}).get("tenant_id")
    if not tenant_id:
        raise HTTPException(404, "No tenant linked to this session")

    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            raise HTTPException(404, "Tenant not found")
        if not t.active:
            t.active = True
            t.subscription_status = "active"
        # Back-fill Stripe IDs if webhook hasn't run yet
        if not t.stripe_customer_id and sess.get("customer"):
            t.stripe_customer_id = sess.get("customer")
        if not t.stripe_subscription_id and sess.get("subscription"):
            t.stripe_subscription_id = sess.get("subscription")
        db.commit()
        return {
            "tenant_id": t.id,
            "tenant_key": t.tenant_key,
            "plan": t.plan,
            "name": t.name,
            "email": t.contact_email,
            "active": t.active,
        }
