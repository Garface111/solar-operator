"""
Solar Operator — onboarding wizard API (June 2026 rebuild).

Replaces the single-shot `POST /v1/signup` with a 5-screen flow:

    1. Welcome & agreement   (frontend only)
    2. Operator info         → POST /v1/onboarding/checkout  → Stripe Checkout
    3. Install extension     → poll GET  /v1/onboarding/extension-ping
                               then POST /v1/onboarding/extension-installed
    4. Add clients           → POST /v1/onboarding/clients
    5. Done                  → POST /v1/onboarding/complete   → magic-link email

Pending state lives on the Tenant row:
  - `onboarding_token`  — 32-char url-safe string, also passed to Stripe as
                          metadata so the post-payment webhook can find the
                          pending tenant.
  - `onboarding_stage`  — pending_payment | extension | clients | done

The Stripe webhook itself lives in `signup.py` (shared with the legacy flow);
it flips a token-bearing tenant to active + stage='extension' WITHOUT sending
the welcome email — that is deferred to `/v1/onboarding/complete`.
"""
from __future__ import annotations

import os
import secrets
import logging
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, Client, Array, UtilitySession, now
from .notify import send_welcome_email, send_internal_alert
from .account import issue_magic_link

logger = logging.getLogger(__name__)


# ─── config ──────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_SETUP_PRICE_ID = os.getenv("STRIPE_SETUP_PRICE_ID", "")  # $250 one-time
STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")  # $45/array/mo
APP_URL = os.getenv("APP_URL", "https://solaroperator.org").rstrip("/")
API_URL = os.getenv("API_URL", "https://web-production-49c83.up.railway.app").rstrip("/")
SETUP_FEE_CENTS = int(os.getenv("ONBOARDING_SETUP_CENTS", "25000"))   # $250 one-time
ARRAY_PRICE_CENTS = int(os.getenv("ONBOARDING_ARRAY_CENTS", "4500"))  # $45/array/mo

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter(prefix="/v1/onboarding")


# ─── schemas ─────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=120)
    company: Optional[str] = Field(None, max_length=200)


class CheckoutResponse(BaseModel):
    checkout_url: str
    onboarding_token: str


class ArrayInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    nepool_gis_id: Optional[str] = Field(None, max_length=20)
    bill_offset_months: Optional[int] = 1


class ClientInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    contact_email: Optional[EmailStr] = None
    gmp_email: Optional[EmailStr] = None
    gmp_autopopulate: bool = False
    arrays: list[ArrayInput] = Field(default_factory=list)


# ─── helpers ─────────────────────────────────────────────────────────────

def gen_tenant_id() -> str:
    return "ten_" + secrets.token_hex(8)


def gen_tenant_key() -> str:
    return "sol_live_" + secrets.token_urlsafe(24)


def gen_onboarding_token() -> str:
    # token_urlsafe(24) → ~32-char url-safe string
    return secrets.token_urlsafe(24)


def _tenant_by_token(db, token: str) -> Tenant:
    """Resolve a pending/active tenant from its onboarding_token or 404."""
    if not token:
        raise HTTPException(400, "Missing onboarding token")
    t = db.execute(
        select(Tenant).where(Tenant.onboarding_token == token)
    ).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Onboarding session not found or expired")
    return t


def _line_items() -> list[dict]:
    """Setup fee (one-time) + per-array subscription. Falls back to inline
    price_data when the price IDs aren't configured (dev mode)."""
    if STRIPE_SETUP_PRICE_ID and STRIPE_ARRAY_PRICE_ID:
        return [
            {"price": STRIPE_SETUP_PRICE_ID, "quantity": 1},
            # Quantity is reconciled to the real array count after Screen 4.
            {"price": STRIPE_ARRAY_PRICE_ID, "quantity": 1},
        ]
    return [
        {
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Solar Operator — setup"},
                "unit_amount": SETUP_FEE_CENTS,
            },
            "quantity": 1,
        },
        {
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Solar Operator — per array"},
                "unit_amount": ARRAY_PRICE_CENTS,
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        },
    ]


# ─── 1. checkout ─────────────────────────────────────────────────────────

@router.post("/checkout", response_model=CheckoutResponse)
def checkout(req: CheckoutRequest):
    """Create a pending tenant + Stripe Checkout session.

    The tenant is inactive (active=False, stage='pending_payment') until the
    Stripe webhook fires on `checkout.session.completed`. The onboarding_token
    is returned to the SPA AND embedded in Stripe metadata so the webhook /
    return path can find this exact pending tenant.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe is not configured on this server")

    email = req.email.lower().strip()
    display_name = (req.company or req.full_name).strip()[:200]

    tenant_id = gen_tenant_id()
    tenant_key = gen_tenant_key()
    onboarding_token = gen_onboarding_token()

    with SessionLocal() as db:
        existing = db.execute(
            select(Tenant).where(Tenant.contact_email == email, Tenant.active == True)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409,
                "An account already exists for this email. "
                "Email support@solaroperator.org if you've lost access.")

        t = Tenant(
            id=tenant_id, name=display_name, contact_email=email,
            tenant_key=tenant_key, plan="standard", active=False, created_at=now(),
            subscription_status="pending",
            onboarding_token=onboarding_token,
            onboarding_stage="pending_payment",
        )
        db.add(t)
        db.commit()

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer_email=email,
            line_items=_line_items(),
            success_url=(
                f"{APP_URL}/onboarding/extension"
                f"?onboarding_token={onboarding_token}"
                f"&session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{APP_URL}/onboarding/info?cancelled=1",
            allow_promotion_codes=True,
            metadata={
                "onboarding_token": onboarding_token,
                "tenant_id": tenant_id,
                "name": req.full_name,
                "company": req.company or "",
            },
            subscription_data={
                "metadata": {
                    "onboarding_token": onboarding_token,
                    "tenant_id": tenant_id,
                },
            },
        )
    except stripe.error.StripeError as e:
        # Roll back the orphaned pending tenant
        with SessionLocal() as db:
            t = db.get(Tenant, tenant_id)
            if t:
                db.delete(t); db.commit()
        raise HTTPException(502, f"Stripe error: {str(e)}")

    return CheckoutResponse(checkout_url=session.url, onboarding_token=onboarding_token)


# ─── 2. status ───────────────────────────────────────────────────────────

@router.get("/status")
def status(token: str = Query(...)):
    """Poll target for the SPA. Returns the current wizard stage plus the
    activation code (tenant_key) once the tenant is active."""
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        n_clients = db.execute(
            select(Client).where(Client.tenant_id == t.id)
        ).scalars().all()
        n_arrays = db.execute(
            select(Array).where(Array.tenant_id == t.id)
        ).scalars().all()
        return {
            "stage": t.onboarding_stage,
            "tenant_id": t.id,
            "active": t.active,
            "activation_code": t.tenant_key if t.active else None,
            "clients_count": len(n_clients),
            "arrays_count": len(n_arrays),
        }


# ─── 3. extension install ────────────────────────────────────────────────

@router.get("/extension-ping")
def extension_ping(token: str = Query(...)):
    """Did a capture land for this tenant in the last 24h? Polled every 3s by
    Screen 3 to auto-advance once the extension has synced."""
    from datetime import timedelta
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        cutoff = now() - timedelta(hours=24)
        sess = db.execute(
            select(UtilitySession)
            .where(UtilitySession.tenant_id == t.id,
                   UtilitySession.captured_at >= cutoff)
            .order_by(UtilitySession.captured_at.desc())
        ).scalars().first()
        return {
            "installed": sess is not None,
            "last_capture_at": sess.captured_at.isoformat() if sess else None,
        }


@router.post("/extension-installed")
def extension_installed(token: str = Query(...)):
    """Manual fallback for Screen 3 — operator clicks "I've installed it".
    Advances the stage to 'clients'."""
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        if t.onboarding_stage in ("pending_payment",):
            # Haven't paid yet — don't let them skip ahead.
            raise HTTPException(402, "Complete payment before installing the extension")
        if t.onboarding_stage == "extension":
            t.onboarding_stage = "clients"
            db.commit()
        return {"ok": True, "stage": t.onboarding_stage}


# ─── billing reconciliation ──────────────────────────────────────────────

def _reconcile_subscription_quantity(
    subscription_id: str, array_count: int, tenant_id: str, email: str
) -> None:
    """Bring the recurring per-array line item up to the real array count.

    The Checkout session creates the subscription with the per-array item at
    quantity=1 (we don't know the count until Screen 4). Here we find that
    recurring item (matching STRIPE_ARRAY_PRICE_ID — NOT the one-time setup
    item, STRIPE_SETUP_PRICE_ID) and bump its quantity, prorating the first
    month.

    Best-effort: if anything fails we log loudly and alert Ford, but we never
    raise — the operator already paid the setup fee, and finishing onboarding
    matters more than a perfectly-reconciled invoice (Ford fixes it manually).
    """
    if not subscription_id:
        logger.error(
            "Cannot reconcile billing for tenant %s — no stripe_subscription_id "
            "on record. Array count = %d.", tenant_id, array_count)
        send_internal_alert(
            "⚠️ Billing not reconciled — missing subscription id",
            f"Tenant {tenant_id} ({email}) finished Screen 4 with {array_count} "
            f"array(s) but has no stripe_subscription_id. Stripe still bills for "
            f"1 array. Fix the subscription quantity manually."
        )
        return

    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        recurring_item = None
        for item in sub["items"]["data"]:
            if item["price"]["id"] == STRIPE_ARRAY_PRICE_ID:
                recurring_item = item
                break
        if recurring_item is None:
            raise RuntimeError(
                f"no line item matching STRIPE_ARRAY_PRICE_ID="
                f"{STRIPE_ARRAY_PRICE_ID!r} on subscription {subscription_id}")

        stripe.SubscriptionItem.modify(
            recurring_item["id"],
            quantity=array_count,
            proration_behavior="create_prorations",
        )
        logger.info(
            "Reconciled subscription %s for tenant %s → quantity=%d",
            subscription_id, tenant_id, array_count)
    except Exception as e:  # noqa: BLE001 — must never block onboarding
        logger.exception(
            "Stripe billing reconciliation FAILED for tenant %s (sub %s, "
            "wanted quantity=%d): %s", tenant_id, subscription_id, array_count, e)
        send_internal_alert(
            "⚠️ Stripe billing reconciliation failed",
            f"Tenant {tenant_id} ({email}) finished Screen 4 with {array_count} "
            f"array(s), but updating subscription {subscription_id} to "
            f"quantity={array_count} failed: {e}\n\n"
            f"Stripe is still billing for 1 array. Fix the quantity manually in "
            f"the Stripe dashboard (proration_behavior=create_prorations)."
        )


# ─── 4. clients ──────────────────────────────────────────────────────────

@router.post("/clients")
def add_clients(clients: list[ClientInput], token: str = Query(...)):
    """Persist the operator's reporting clients (+ any manually-entered
    arrays). GMP autopopulate is recorded here; the actual array import
    happens later in the /v1/sync handler when the extension captures a
    matching login."""
    if not clients:
        raise HTTPException(400, "Provide at least one client")

    created_ids: list[int] = []
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        if not t.active:
            raise HTTPException(402, "Payment not complete")

        for ci in clients:
            name = ci.name.strip()
            dupe = db.execute(
                select(Client).where(Client.tenant_id == t.id, Client.name == name)
            ).scalar_one_or_none()
            if dupe:
                raise HTTPException(409, f"A client named '{name}' already exists")
            c = Client(
                tenant_id=t.id,
                name=name,
                contact_email=ci.contact_email,
                gmp_email=(ci.gmp_email.lower().strip() if ci.gmp_email else None),
                gmp_autopopulate=bool(ci.gmp_autopopulate),
                active=True,
            )
            db.add(c); db.flush()
            created_ids.append(c.id)

            for ai in ci.arrays:
                aname = ai.name.strip()
                if not aname:
                    continue
                arr_dupe = db.execute(
                    select(Array).where(Array.tenant_id == t.id, Array.name == aname)
                ).scalar_one_or_none()
                if arr_dupe:
                    raise HTTPException(409, f"An array named '{aname}' already exists")
                db.add(Array(
                    tenant_id=t.id, client_id=c.id, name=aname,
                    nepool_gis_id=ai.nepool_gis_id,
                    bill_offset_months=(ai.bill_offset_months
                                        if ai.bill_offset_months is not None else 1),
                ))

        if t.onboarding_stage in ("extension", "clients"):
            t.onboarding_stage = "clients"
        db.commit()

        # Snapshot what we need for billing while the session is still open.
        subscription_id = t.stripe_subscription_id
        tenant_id = t.id
        tenant_email = t.contact_email
        array_count = len(db.execute(
            select(Array).where(Array.tenant_id == t.id)
        ).scalars().all())

    # Now that every client + array is persisted, bring Stripe in line with the
    # real array count. Best-effort — never blocks the operator reaching Screen 5.
    _reconcile_subscription_quantity(
        subscription_id, array_count, tenant_id, tenant_email)

    return {"ok": True, "client_ids": created_ids}


# ─── 5. complete ─────────────────────────────────────────────────────────

@router.post("/complete")
def complete(token: str = Query(...)):
    """Finish onboarding: mark stage='done', send the deferred welcome email,
    and fire a magic-link sign-in email so the operator can reach the
    dashboard (reuses account.py's auth flow)."""
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        if not t.active:
            raise HTTPException(402, "Payment not complete")
        t.onboarding_stage = "done"
        db.commit()
        email = t.contact_email
        name = t.name
        tenant_key = t.tenant_key
        plan = t.plan
        tenant_id = t.id

    # Deferred welcome email (NOT sent by the webhook for onboarding-flow tenants).
    try:
        send_welcome_email(to=email, name=name, tenant_key=tenant_key, plan=plan)
    except Exception as e:
        send_internal_alert(
            "Onboarding welcome email failed",
            f"Tenant {tenant_id} ({email}) finished onboarding but the welcome "
            f"email failed: {e}. Send manually."
        )

    # Magic-link sign-in so they can open the dashboard immediately.
    magic_link_email_sent = issue_magic_link(email)

    send_internal_alert(
        "🌞 Onboarding complete",
        f"Tenant {tenant_id} ({email}) finished the onboarding wizard."
    )

    return {"ok": True, "magic_link_email_sent": magic_link_email_sent}
