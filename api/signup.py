"""
Solar Operator — signup + Stripe checkout + webhook handlers.

Public flow:
  POST /v1/signup            → creates a "pending" tenant + Stripe Checkout session,
                               returns the checkout URL for the frontend to redirect to.
  POST /v1/stripe/webhook    → Stripe → tenant activation + welcome email on
                               successful checkout.

Test card (Stripe test mode): 4242 4242 4242 4242  any future date  any CVC
"""
from __future__ import annotations

import os
import secrets
import re
from typing import Optional

import stripe
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, now
from .notify import send_welcome_email, send_internal_alert


# ─── config ──────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
APP_URL = os.getenv("APP_URL", "https://solaroperator.org").rstrip("/")

# Plans: name → (Stripe-friendly product name, monthly price in cents)
PLANS = {
    "solo":     ("Solar Operator — Solo",     4900,  "Up to 2 GMP meters"),
    "manager":  ("Solar Operator — Manager",  9900,  "Up to 10 GMP meters · most popular"),
    "operator": ("Solar Operator — Operator", 24900, "Unlimited meters · multi-utility"),
}

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter()


# ─── schemas ─────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    plan: str = Field(..., pattern="^(solo|manager|operator)$")
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    company: Optional[str] = Field(None, max_length=200)


class SignupResponse(BaseModel):
    checkout_url: str
    tenant_id: str


# ─── helpers ─────────────────────────────────────────────────────────────

def gen_tenant_id() -> str:
    return "ten_" + secrets.token_hex(8)


def gen_tenant_key() -> str:
    # 32-char URL-safe token + sol_live_ prefix
    return "sol_live_" + secrets.token_urlsafe(24)


# ─── public: signup ──────────────────────────────────────────────────────

@router.post("/v1/signup", response_model=SignupResponse)
def signup(req: SignupRequest):
    """Create a pending tenant + Stripe Checkout session.

    Tenant is marked active=False until webhook fires on successful payment.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe is not configured on this server")

    if req.plan not in PLANS:
        raise HTTPException(400, "Unknown plan")
    product_name, price_cents, plan_blurb = PLANS[req.plan]

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
            tenant_key=tenant_key, plan=req.plan, active=False, created_at=now(),
        )
        db.add(t)
        db.commit()

    # Build Stripe Checkout session
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer_email=req.email,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": product_name,
                        "description": plan_blurb,
                    },
                    "unit_amount": price_cents,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            success_url=f"{APP_URL}/welcome.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/signup.html?cancelled=1",
            metadata={
                "tenant_id": tenant_id,
                "plan": req.plan,
                "name": req.name,
                "company": req.company or "",
            },
            subscription_data={
                "metadata": {"tenant_id": tenant_id, "plan": req.plan},
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

@router.post("/v1/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default=None)):
    """Receives Stripe events. Activates tenant + sends welcome email on
    checkout.session.completed."""
    payload = await request.body()

    if not STRIPE_WEBHOOK_SECRET:
        # Soft-fail in dev: parse without verification but log loudly
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

    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        tenant_id = (sess.get("metadata") or {}).get("tenant_id")
        if not tenant_id:
            return {"ok": True, "ignored": "no tenant_id in metadata"}

        with SessionLocal() as db:
            t = db.get(Tenant, tenant_id)
            if not t:
                send_internal_alert(
                    "Stripe success for unknown tenant",
                    f"Stripe checkout.session.completed fired with metadata "
                    f"tenant_id={tenant_id} but no such tenant in DB. "
                    f"Email on session: {sess.get('customer_email')}"
                )
                return {"ok": True, "ignored": "tenant not found"}

            t.active = True
            db.commit()
            tenant_key = t.tenant_key
            tenant_name = t.name
            tenant_email = t.contact_email
            tenant_plan = t.plan

        # Send welcome email with activation code
        try:
            send_welcome_email(
                to=tenant_email,
                name=(sess.get("metadata") or {}).get("name") or tenant_name,
                tenant_key=tenant_key,
                plan=tenant_plan,
            )
        except Exception as e:
            send_internal_alert(
                "Welcome email failed",
                f"Tenant {tenant_id} ({tenant_email}) paid successfully but "
                f"welcome email failed: {e}. Send manually."
            )

        # Notify ourselves so we can prep their sheet mapping
        send_internal_alert(
            "🌞 New Solar Operator signup",
            f"Name: {sess.get('metadata',{}).get('name','?')}\n"
            f"Email: {tenant_email}\n"
            f"Company: {sess.get('metadata',{}).get('company','—')}\n"
            f"Plan: {tenant_plan}\n"
            f"Tenant ID: {tenant_id}\n\n"
            f"Reply to the customer's welcome email and ask for their "
            f"reporting spreadsheet so you can wire their sheet-mapper."
        )

        return {"ok": True, "tenant_activated": tenant_id}

    # Ignore other events for now (invoice.paid, customer.subscription.updated…)
    return {"ok": True, "event": event["type"], "handled": False}


# ─── public: fetch activation code by Stripe session id ─────────────────

@router.get("/v1/checkout/{session_id}")
def checkout_lookup(session_id: str):
    """Called by the post-payment welcome page to show the activation code
    inline (no email dependency). Verifies the session was actually paid,
    then returns the tenant_key. Self-heals if the webhook lagged or failed
    by activating the tenant on first lookup.
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
        # Self-heal: webhook may not have fired yet (or signature mismatch).
        # We've confirmed payment_status=paid via Stripe directly, so it's
        # safe to activate here.
        if not t.active:
            t.active = True
            db.commit()
        return {
            "tenant_id": t.id,
            "tenant_key": t.tenant_key,
            "plan": t.plan,
            "name": t.name,
            "email": t.contact_email,
            "active": t.active,
        }
