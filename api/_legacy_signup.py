"""
NEPOOL Operator — LEGACY signup + Stripe checkout endpoints.

⚠️  RETIRED at v1.1.0. This module is no longer mounted (see api/app.py). It was
renamed from `api/signup.py` when the single-shot `POST /v1/signup` flow was
replaced by the 5-screen onboarding wizard in `api/onboarding.py`.

What moved OUT of this file at the cutover:
  - The shared Stripe webhook (`POST /v1/stripe/webhook`) and all its
    `_process_*` lifecycle handlers now live in `api/stripe_webhook.py`, which IS
    mounted. The webhook still handles both onboarding-token sessions and any
    in-flight legacy `tenant_id` sessions.

What stayed here (dead code, kept for reference / emergency re-mount only):
  POST /v1/signup            → created a "pending" tenant + Stripe Checkout session.
  GET  /v1/checkout/{sid}    → post-payment lookup: showed activation code inline.

Single-plan model (June 2026): one product, $75/month, unlimited arrays. The
legacy frontend sends `plan: "standard"` or omits the field; both were accepted.
"""
from __future__ import annotations

import os
import secrets
import logging
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, now

logger = logging.getLogger(__name__)


# ─── config ──────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")  # price_xxx — set in Stripe dashboard
APP_URL = os.getenv("APP_URL", "https://nepooloperator.com").rstrip("/")
API_URL = os.getenv("API_URL", "https://web-production-49c83.up.railway.app").rstrip("/")
PRICE_CENTS = int(os.getenv("PLAN_PRICE_CENTS", "7500"))  # $75/mo fallback
PLAN_NAME = os.getenv("PLAN_NAME", "NEPOOL Operator")
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
                "Email admin@solaroperator.org if you've lost access.")

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
