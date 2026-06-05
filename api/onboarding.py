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
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, func

from .db import SessionLocal
from .models import Tenant, Client, Array, UtilitySession, now
from .notify import (
    send_welcome_email, send_internal_alert, send_sample_workbook_email,
)
from .account import issue_magic_link, mint_session_for_tenant, tenant_from_session
from .stripe_helpers import reconcile_subscription_quantity

logger = logging.getLogger(__name__)


# ─── config ──────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_SETUP_PRICE_ID = os.getenv("STRIPE_SETUP_PRICE_ID", "")  # $250 one-time
STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")  # $15/array/mo
APP_URL = os.getenv("APP_URL", "https://solaroperator.org").rstrip("/")
API_URL = os.getenv("API_URL", "https://web-production-49c83.up.railway.app").rstrip("/")
# Public, buyer-facing onboarding URL. Netlify 200-proxies solaroperator.org/onboarding
# to the FastAPI /onboarding/* mount on Railway, so Stripe return URLs keep the
# operator on the marketing domain instead of the raw Railway host.
PUBLIC_ONBOARDING_URL = os.getenv("PUBLIC_ONBOARDING_URL", f"{APP_URL}/onboarding").rstrip("/")
SETUP_FEE_CENTS = int(os.getenv("ONBOARDING_SETUP_CENTS", "25000"))   # $250 one-time
ARRAY_PRICE_CENTS = int(os.getenv("ONBOARDING_ARRAY_CENTS", "1500"))  # $15/array/mo

stripe.api_key = STRIPE_SECRET_KEY


def ensure_placeholder_client(db, tenant_id: str) -> Optional[int]:
    """Seed a single 'Your first client' Client when the tenant has none.

    Called on tenant activation. If the operator pre-entered clients during
    onboarding (Path A), this is a no-op. If they used the array-count-only
    path (Path B), this drops a placeholder so:
      - the dashboard has somewhere to anchor the first-visit walkthrough
      - autopop has a Client to bind captured Arrays into (the operator just
        needs to paste their utility-login email into the placeholder and
        toggle autopop ON)
      - the spreadsheet importer has a Client target

    Returns the new Client.id, or None if a real client already existed.
    """
    existing = db.execute(
        select(Client).where(
            Client.tenant_id == tenant_id,
            Client.deleted_at.is_(None),
        )
    ).first()
    if existing:
        return None
    placeholder = Client(
        tenant_id=tenant_id,
        name="Your first client",
        active=True,
        is_placeholder=True,
        # GMP autopop on by default — the whole point of the placeholder
        # is that the operator's first portal login auto-fills this client.
        gmp_autopopulate=True,
        vec_autopopulate=True,
    )
    db.add(placeholder)
    db.flush()
    return placeholder.id

router = APIRouter(prefix="/v1/onboarding")


# ─── schemas ─────────────────────────────────────────────────────────────

class ArraySeed(BaseModel):
    """Pre-checkout array entry (Path A). Simpler than ArrayInput — no GMP
    fields needed before payment."""
    name: str = Field(..., min_length=1, max_length=120)
    nepool_gis_id: Optional[str] = Field(None, max_length=20)
    bill_offset_months: Optional[int] = 1


class ClientSeed(BaseModel):
    """Pre-checkout client entry (Path A). GMP credentials are added later on
    the Clients screen post-payment."""
    name: str = Field(..., min_length=1, max_length=200)
    contact_email: Optional[EmailStr] = None
    arrays: list[ArraySeed] = Field(default_factory=list)


class CheckoutRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=120)
    company: Optional[str] = Field(None, max_length=200)
    # N4: honest checkout pricing.
    # Path A — operator enters clients+arrays before paying; quantity = real count.
    clients: Optional[list[ClientSeed]] = None
    # Path B — operator provides an estimate; quantity syncs to reality later.
    array_count: Optional[int] = Field(None, ge=1)


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
    gmp_username: Optional[str] = Field(None, max_length=120)
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


def _line_items(quantity: int = 1) -> list[dict]:
    """Setup fee (one-time) + per-array subscription at the given quantity.

    Falls back to inline price_data when the price IDs aren't configured (dev
    mode). quantity defaults to 1 for backwards compat with old SPA builds that
    don't send array_count or clients."""
    if STRIPE_SETUP_PRICE_ID and STRIPE_ARRAY_PRICE_ID:
        return [
            {"price": STRIPE_SETUP_PRICE_ID, "quantity": 1},
            {"price": STRIPE_ARRAY_PRICE_ID, "quantity": quantity},
        ]
    return [
        {
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Solar Operator — one-time setup"},
                "unit_amount": SETUP_FEE_CENTS,
            },
            "quantity": 1,
        },
        {
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Solar Operator — monthly per-array fee"},
                "unit_amount": ARRAY_PRICE_CENTS,
                "recurring": {"interval": "month"},
            },
            "quantity": quantity,
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

    # N4: derive Stripe line-item quantity from pre-entered clients (Path A) or
    # operator estimate (Path B). Old SPA builds send neither → quantity=1.
    if req.clients is not None:
        quantity = max(1, sum(len(c.arrays) for c in req.clients))
    elif req.array_count is not None:
        quantity = max(1, req.array_count)
    else:
        quantity = 1

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

        # Path A: persist clients + arrays before payment so the Stripe quantity
        # matches the real count from the moment the subscription is created.
        # Cascade delete on Tenant handles cleanup if Stripe session creation fails.
        if req.clients:
            for ci in req.clients:
                cname = ci.name.strip()
                if not cname:
                    continue
                c = Client(
                    tenant_id=tenant_id,
                    name=cname,
                    contact_email=ci.contact_email,
                    # Default GMP+VEC autopop ON — operator's first portal
                    # login populates arrays without an extra opt-in step.
                    gmp_autopopulate=True,
                    vec_autopopulate=True,
                    active=True,
                )
                db.add(c)
                db.flush()  # get c.id for the Array FK
                for ai in ci.arrays:
                    aname = ai.name.strip()
                    if aname:
                        db.add(Array(
                            tenant_id=tenant_id,
                            client_id=c.id,
                            name=aname,
                            nepool_gis_id=ai.nepool_gis_id,
                            bill_offset_months=(
                                ai.bill_offset_months
                                if ai.bill_offset_months is not None else 1
                            ),
                        ))

        db.commit()

    try:
        session = stripe.checkout.Session.create(
            mode="setup",
            payment_method_types=["card"],
            customer_email=email,
            setup_intent_data={
                "metadata": {
                    "onboarding_token": onboarding_token,
                    "tenant_id": tenant_id,
                },
            },
            success_url=(
                f"{PUBLIC_ONBOARDING_URL}/extension"
                f"?onboarding_token={onboarding_token}"
                f"&session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{PUBLIC_ONBOARDING_URL}/info?cancelled=1",
            metadata={
                "onboarding_token": onboarding_token,
                "tenant_id": tenant_id,
                "name": req.full_name,
                "company": req.company or "",
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

def _status_payload(db, t: Tenant) -> dict:
    """Shared status dict for /status and /reconcile-checkout."""
    n_clients = db.execute(
        select(func.count()).select_from(Client).where(Client.tenant_id == t.id)
    ).scalar() or 0
    n_arrays = db.execute(
        select(func.count()).select_from(Array).where(Array.tenant_id == t.id)
    ).scalar() or 0
    from datetime import datetime as _dt
    hb = t.extension_heartbeat_at
    # "extension_active" = heartbeat received within the last 2 minutes.
    extension_active = (
        hb is not None
        and (_dt.utcnow() - hb).total_seconds() < 120
    )
    return {
        "stage": t.onboarding_stage,
        "tenant_id": t.id,
        "active": t.active,
        "activation_code": t.tenant_key if t.active else None,
        "clients_count": int(n_clients),
        "arrays_count": int(n_arrays),
        "extension_active": extension_active,
        "extension_heartbeat_at": hb.isoformat() if hb else None,
    }


@router.get("/status")
def status(token: str = Query(...)):
    """Poll target for the SPA. Returns the current wizard stage plus the
    activation code (tenant_key) once the tenant is active."""
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        return _status_payload(db, t)


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


@router.post("/test-connection")
def test_connection(token: str = Query(...)):
    """In-flow 'Test connection' (V3): has the extension actually reached us?

    Returns whether a UtilitySession landed for this tenant in the last 5
    minutes (connected), the total capture count, and the most recent capture
    time. Lets the operator confirm the extension + activation code are wired up
    instead of sitting on Screen 3 wondering whether anything is happening."""
    from datetime import timedelta
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        sessions = db.execute(
            select(UtilitySession)
            .where(UtilitySession.tenant_id == t.id)
            .order_by(UtilitySession.captured_at.desc())
        ).scalars().all()
        last = sessions[0] if sessions else None
        cutoff = now() - timedelta(minutes=5)
        return {
            "connected": bool(last and last.captured_at >= cutoff),
            "captures_count": len(sessions),
            "last_capture_at": last.captured_at.isoformat() if last else None,
        }


def _activate_from_paid_session(token: str, session_id: Optional[str]) -> bool:
    """Self-heal a paid-but-inactive tenant when the Stripe webhook is lagging.

    The Checkout success_url carries both `onboarding_token` and `session_id`.
    If the tenant is still inactive, look the Checkout session up directly and,
    if Stripe reports it `paid`, activate the tenant and advance to the
    'extension' stage — exactly what the webhook would have done.

    Idempotent (a no-op once active) and never raises: returns True iff the
    tenant is active afterward. The session must belong to this onboarding
    token, so a stray/forged session_id can't activate someone else's tenant.
    """
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        if t.active:
            return True
        token_val = t.onboarding_token

    if not session_id or not STRIPE_SECRET_KEY:
        return False

    try:
        sess = stripe.checkout.Session.retrieve(session_id)
    except Exception:  # noqa: BLE001 — never block onboarding on a Stripe hiccup
        logger.exception("reconcile: Stripe session retrieve failed for %s", session_id)
        return False

    meta = sess.get("metadata") or {}
    if meta.get("onboarding_token") and meta.get("onboarding_token") != token_val:
        logger.warning("reconcile: session %s does not belong to token", session_id)
        return False
    mode = sess.get("mode", "subscription")
    if mode == "setup":
        if sess.get("status") != "complete":
            return False
    else:
        if sess.get("payment_status") != "paid":
            return False

    from datetime import timedelta
    customer = sess.get("customer")
    subscription = sess.get("subscription")
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        if not t.active:
            t.active = True
            if mode == "setup":
                t.subscription_status = "trialing"
                if t.trial_ends_at is None:
                    t.trial_ends_at = now() + timedelta(days=14)
            else:
                t.subscription_status = "active"
            if t.onboarding_stage == "pending_payment":
                t.onboarding_stage = "extension"
            if customer:
                t.stripe_customer_id = customer
            if subscription:
                t.stripe_subscription_id = subscription
            # Seed a placeholder client if Path B (array-count-only).
            ensure_placeholder_client(db, t.id)
            db.commit()
            logger.info("reconcile: self-healed tenant %s from paid session %s",
                        t.id, session_id)
    return True


@router.post("/reconcile-checkout")
def reconcile_checkout(token: str = Query(...), session_id: str = Query(...)):
    """Self-heal endpoint for Screen 3: verify the Stripe Checkout session and,
    if paid, activate the tenant. Safe to call repeatedly while the webhook is
    in flight. Always returns the current onboarding status — never 402s a
    tenant we just redirected back from Checkout."""
    _activate_from_paid_session(token, session_id)
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        return _status_payload(db, t)


@router.post("/extension-installed")
def extension_installed(token: str = Query(...),
                        session_id: Optional[str] = Query(default=None)):
    """Manual fallback for Screen 3 — operator clicks "I've installed it".
    Advances the stage to 'clients'."""
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        stage = t.onboarding_stage
    if stage == "pending_payment":
        # Webhook may simply be lagging. If we hold a paid Checkout session,
        # self-heal instead of telling a paying customer to pay again.
        if not _activate_from_paid_session(token, session_id):
            raise HTTPException(402, "Complete payment before installing the extension")
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        if t.onboarding_stage == "extension":
            t.onboarding_stage = "clients"
            db.commit()
        return {"ok": True, "stage": t.onboarding_stage}


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
                gmp_username=(ci.gmp_username.strip() if ci.gmp_username and ci.gmp_username.strip() else None),
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

        # ── Drop the seed placeholder Client(s) (Path A activation) ─────
        # When the operator went through Path A and manually entered real
        # clients here, the "Your first client" placeholder Client (seeded
        # at Stripe webhook time) is now redundant. Delete it cleanly so
        # the dashboard doesn't show a phantom row alongside the real
        # clients. The placeholder by construction has no Arrays, so it's
        # safe to delete without orphaning.
        placeholders = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.is_placeholder.is_(True),
            )
        ).scalars().all()
        for ph in placeholders:
            db.delete(ph)

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
    # real array count. This runs SYNCHRONOUSLY before we return (W2-11) so the
    # Done screen's reconciled count + monthly total are accurate the moment the
    # SPA advances. Best-effort — never blocks the operator reaching Screen 5.
    reconcile_subscription_quantity(
        subscription_id, array_count, tenant_id, tenant_email)

    return {"ok": True, "client_ids": created_ids, "array_count": array_count}


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

    # Log them straight into the dashboard: mint a session bound to this tenant
    # and hand it back so the SPA can stash it in localStorage. The user just
    # paid us — don't force a detour through their email to reach the dashboard.
    session_token = mint_session_for_tenant(tenant_id)

    # Magic-link sign-in too, for when they later sign in from a new browser
    # or device (the session token above only lives in this browser).
    magic_link_email_sent = issue_magic_link(email)

    # Second email: a sample report so they see what their clients will receive.
    # Best-effort — already swallows its own exceptions, never blocks completion.
    sample_email_sent = send_sample_workbook_email(
        to=email, name=name, dashboard_url=f"{APP_URL}/accounts")

    send_internal_alert(
        "🌞 Onboarding complete",
        f"Tenant {tenant_id} ({email}) finished the onboarding wizard."
    )

    return {"ok": True, "session_token": session_token,
            "magic_link_email_sent": magic_link_email_sent,
            "sample_email_sent": sample_email_sent}


# ─── 6. cancel trial ─────────────────────────────────────────────────────

@router.post("/cancel-trial")
def cancel_trial(authorization: Optional[str] = Header(default=None)):
    """Cancel the trial before it ends. Free, one-click, no questions.

    Auth: session Bearer token (same as /v1/account/*). Detaches the stored
    payment method from Stripe and marks the tenant cancelled. Must be called
    while subscription_status='trialing'; post-trial cancellations go through
    Stripe Billing Portal.
    """
    t = tenant_from_session(authorization)
    if t.subscription_status != "trialing":
        raise HTTPException(400, "No active trial to cancel")

    pm_id = t.stripe_payment_method_id
    cus_id = t.stripe_customer_id

    if STRIPE_SECRET_KEY and pm_id:
        try:
            stripe.PaymentMethod.detach(pm_id)
        except Exception:
            logger.exception("Could not detach payment method %s for tenant %s",
                             pm_id, t.id)

    with SessionLocal() as db:
        tenant = db.get(Tenant, t.id)
        if tenant:
            tenant.active = False
            tenant.subscription_status = "cancelled"
            tenant.trial_ends_at = None
            tenant.stripe_payment_method_id = None
            db.commit()

    send_internal_alert(
        f"Trial cancelled: {t.id}",
        f"Tenant {t.id} ({t.contact_email}) cancelled their trial. "
        f"PM {pm_id} detached. Customer: {cus_id}"
    )
    return {"ok": True}
