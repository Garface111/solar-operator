"""
NEPOOL Operator — onboarding wizard API (June 2026 rebuild).

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
from datetime import timedelta
from typing import Optional

import stripe
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, func

from . import branding
from .db import SessionLocal
from .fuels import normalize_fuel
from .models import Tenant, Client, Array, UtilitySession, now
from .notify import (
    send_welcome_email, send_internal_alert, send_sample_workbook_email,
    send_trial_welcome_email,
)
from .account import (
    require_not_demo,
    issue_magic_link,
    mint_session_for_tenant,
    tenant_from_session,
    _hash_password,
    _validate_password_strength,
)
from .stripe_helpers import reconcile_subscription_quantity, billable_array_count

logger = logging.getLogger(__name__)


# ─── config ──────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_SETUP_PRICE_ID = os.getenv("STRIPE_SETUP_PRICE_ID", "")  # $250 one-time
STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")  # $15/array/mo
APP_URL = os.getenv("APP_URL", "https://nepooloperator.com").rstrip("/")
API_URL = os.getenv("API_URL", "https://web-production-49c83.up.railway.app").rstrip("/")
# Public, buyer-facing onboarding URL. Netlify 200-proxies nepooloperator.com/onboarding
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
    # checkout_url is now always None (no card is collected at signup). Kept in
    # the response shape so any stale wizard bundle still in a browser tab can
    # deserialize the response without crashing.
    checkout_url: Optional[str] = None
    onboarding_token: str
    tenant_id: Optional[str] = None


class StartRequest(BaseModel):
    """No-upfront-payment signup. No card is collected — the operator drops
    straight into a 14-day trial and adds a card later from the dashboard."""
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=120)
    company: Optional[str] = Field(None, max_length=200)
    password: Optional[str] = None
    array_count: Optional[int] = Field(None, ge=1)
    # Which EnergyAgent product this signup is for. Defaults to the NEPOOL
    # verifier; the Array Operator owner site posts "array_operator" so the
    # tenant bills on the owner price. Same 14-day trial either way.
    product: Optional[str] = Field("nepool", pattern="^(nepool|array_operator)$")
    # Terms/Privacy + account-access authorization version the user accepted at
    # signup (the consent checkbox). Persisted on the tenant as proof of consent.
    consent_version: Optional[str] = Field(None, max_length=40)


class StartResponse(BaseModel):
    onboarding_token: str
    tenant_id: str


class ArrayInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    nepool_gis_id: Optional[str] = Field(None, max_length=20)
    bill_offset_months: Optional[int] = 1
    # V2 (feat/v2-rec-fuels): generation source — solar|wind|hydro|digester|
    # storage. Omitted by the wizard for solar (the byte-identical default).
    # Normalized server-side, so an unknown value degrades to solar.
    fuel_type: Optional[str] = Field(None, max_length=20)


class ClientInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    contact_email: Optional[EmailStr] = None
    gmp_email: Optional[EmailStr] = None
    gmp_username: Optional[str] = Field(None, max_length=120)
    gmp_autopopulate: bool = False
    # V2: the client's default fuel. Applied to manually-entered arrays that
    # don't carry their own fuel_type, and persisted on the Client so arrays
    # auto-populated later by /v1/sync (the autopop path, which sends no arrays
    # here) inherit the fuel the operator picked during onboarding.
    default_fuel_type: Optional[str] = Field(None, max_length=20)
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
                "product_data": {"name": "NEPOOL Operator — one-time setup"},
                "unit_amount": SETUP_FEE_CENTS,
            },
            "quantity": 1,
        },
        {
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "NEPOOL Operator — monthly per-array fee"},
                "unit_amount": ARRAY_PRICE_CENTS,
                "recurring": {"interval": "month"},
            },
            "quantity": quantity,
        },
    ]


# ─── 1. start (no-upfront-payment signup) ────────────────────────────────

def _create_trial_tenant(
    *, email: str, full_name: str, company: Optional[str],
    password: Optional[str], array_count: Optional[int],
    product: str = "nepool",
    consent_version: Optional[str] = None, consent_ip: Optional[str] = None,
) -> tuple[str, str]:
    """Create a live, trialing tenant — no card, no Stripe call.

    The operator is `active=True` and `subscription_status='trialing'` the
    moment they finish signup, with a 14-day trial clock. They add a card later
    from the dashboard (see account.add_payment_method). Returns
    (onboarding_token, tenant_id).

    Raises HTTPException(409) if an active tenant already owns the email, or
    HTTPException(400) if a supplied password is too weak.
    """
    email = email.lower().strip()
    display_name = (company or full_name).strip()[:200]

    # Validate the password up front so a weak one 400s before we touch the DB.
    if password:
        _validate_password_strength(password)

    tenant_id = gen_tenant_id()
    tenant_key = gen_tenant_key()
    onboarding_token = gen_onboarding_token()

    with SessionLocal() as db:
        # NOTE: .first() (not scalar_one_or_none) on purpose — legacy/raced data
        # can leave >1 tenant on the same email, and scalar_one_or_none() raises
        # MultipleResultsFound -> 500, wedging signup permanently for that email.
        #
        # Block a duplicate within the SAME product whether it's active OR
        # inactive: a second signup on an email that already has (say) an
        # array_operator tenant must 409, not silently mint a second one — that
        # duplication is the root cause of the magic-link/password "wrong account"
        # glitches. A different product on the same email is allowed (one person
        # can legitimately own a NEPOOL account AND an Array Operator account).
        existing = db.execute(
            select(Tenant)
            .where(Tenant.contact_email == email, Tenant.product == product)
            .order_by(Tenant.active.desc(), Tenant.created_at.desc())
        ).scalars().first()
        if existing:
            # A DEACTIVATED account is recoverable, not a dead end: signing in
            # reaches /account (we let inactive tenants in) where they can
            # reactivate. Frame it as "welcome back", not an error — a hard
            # "account already exists / lost access?" message reads like a wall
            # for someone whose account is simply paused.
            if existing.active:
                raise HTTPException(409,
                    "An account already exists for this email. "
                    "Sign in instead, or email admin@solaroperator.org if you've lost access.")
            raise HTTPException(409,
                "Welcome back — your account is still here. Sign in to pick up "
                "right where you left off and reactivate it. "
                "Lost access? Email admin@solaroperator.org.")

        t = Tenant(
            id=tenant_id, name=display_name, contact_email=email,
            operator_name=full_name.strip()[:120],
            # Only store a REAL company the owner gave us — don't seed company_name
            # with the email-derived full_name, otherwise Master Account → Company
            # renders pre-filled with a junk "<vendor> owner"/name and the owner has
            # to clear it. Leave it blank so the field shows its "Add your company
            # name" placeholder. `name` (display_name) still falls back to full_name,
            # and every company_name consumer already falls back to name/operator_name.
            company_name=((company or "").strip()[:200] or None),
            tenant_key=tenant_key, plan="standard", active=True, created_at=now(),
            product=product,
            subscription_status="trialing",
            trial_ends_at=now() + timedelta(days=14),
            onboarding_token=onboarding_token,
            onboarding_stage="extension",
            onboarding_array_estimate=array_count,
            # Proof of consent: the Terms/Privacy + account-access authorization
            # the owner accepted at signup (NULL only for the deprecated paths).
            consent_version=consent_version,
            consent_at=(now() if consent_version else None),
            consent_ip=consent_ip,
            # No card on file yet — these stay NULL until the operator adds a
            # payment method from the dashboard.
            stripe_customer_id=None,
            stripe_payment_method_id=None,
            stripe_subscription_id=None,
        )
        if password:
            t.password_hash = _hash_password(password)
        db.add(t)
        db.flush()
        # Seed the "Your first client" placeholder so the dashboard has somewhere
        # to anchor the first-visit walkthrough and autopop has a target. Real
        # clients entered later (onboarding /clients) delete this placeholder.
        ensure_placeholder_client(db, tenant_id)
        db.commit()

    return onboarding_token, tenant_id


@router.post("/start", response_model=StartResponse)
def start(req: StartRequest, request: Request, background_tasks: BackgroundTasks):
    """Begin onboarding with NO upfront payment.

    Creates a live, trialing tenant and returns its onboarding token. No card is
    collected — the 14-day trial starts immediately and the operator adds a
    payment method later from the Accounts tab.
    """
    # Each signup creates a tenant + fires welcome/trial emails — throttle per-IP
    # so the endpoint can't be used to mass-create accounts or spam emails.
    from . import ratelimit
    ratelimit.enforce(request, "onboarding_start_ip", max_hits=10, window_s=600,
                      message="Too many signups from your network — please try again in a few minutes.")
    onboarding_token, tenant_id = _create_trial_tenant(
        email=req.email, full_name=req.full_name, company=req.company,
        password=req.password, array_count=req.array_count,
        product=req.product or "nepool",
        consent_version=req.consent_version,
        consent_ip=ratelimit.client_ip(request),
    )
    # Internal alert is non-critical — send it AFTER the response so the Resend
    # round-trip doesn't hold a request thread under a signup burst.
    background_tasks.add_task(
        send_internal_alert,
        "🌞 New trial started (no card)",
        f"Tenant {tenant_id} ({req.email.lower().strip()}) started a 14-day "
        f"trial. Product: {req.product or 'nepool'}. No card on file. "
        f"Array estimate: {req.array_count}.",
    )
    return StartResponse(onboarding_token=onboarding_token, tenant_id=tenant_id)


# ─── 1b. checkout (DEPRECATED shim) ──────────────────────────────────────

@router.post("/checkout", response_model=CheckoutResponse)
def checkout(req: CheckoutRequest):
    """DEPRECATED. Card collection was removed from signup (no-upfront-payment).

    Kept mounted only so a stale wizard bundle still open in a browser tab
    doesn't crash mid-signup. Does the same tenant creation as /start and
    returns checkout_url=None — there is no Stripe Checkout to redirect to.
    """
    logger.warning(
        "DEPRECATED /v1/onboarding/checkout called (use /v1/onboarding/start). "
        "email=%s", req.email,
    )
    # Old Path A bundles sent pre-entered clients; Path B sent array_count.
    # We only need the count now (real clients are added post-signup).
    if req.clients is not None:
        array_count: Optional[int] = sum(len(c.arrays) for c in req.clients) or None
    else:
        array_count = req.array_count

    onboarding_token, tenant_id = _create_trial_tenant(
        email=req.email, full_name=req.full_name, company=req.company,
        password=None, array_count=array_count,
    )
    return CheckoutResponse(
        checkout_url=None, onboarding_token=onboarding_token, tenant_id=tenant_id)


# ─── 2. status ───────────────────────────────────────────────────────────

def _status_payload(db, t: Tenant) -> dict:
    """Shared status dict for /status and /reconcile-checkout."""
    n_clients = db.execute(
        select(func.count()).select_from(Client).where(Client.tenant_id == t.id)
    ).scalar() or 0
    n_arrays = db.execute(
        select(func.count()).select_from(Array).where(Array.tenant_id == t.id)
    ).scalar() or 0
    from datetime import datetime as _dt, timezone as _tz
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
        # Naive UTC in the DB → stamp UTC offset so the browser doesn't parse it
        # as local time (see account._iso_utc for the full rationale).
        "extension_heartbeat_at": (
            hb.replace(tzinfo=_tz.utc).isoformat()
            if hb is not None and hb.tzinfo is None
            else (hb.isoformat() if hb else None)
        ),
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
def reconcile_checkout(token: str = Query(...),
                       session_id: Optional[str] = Query(default=None)):
    """No-op for the no-upfront-payment flow: tenants are already active=True the
    moment they finish signup, so there's nothing to reconcile. Kept mounted
    because stale extension popups still POST here — just echo current state and
    never 500/402 them. (Legacy in-flight Stripe Checkout sessions, if any, are
    still self-healed via _activate_from_paid_session.)"""
    _activate_from_paid_session(token, session_id)
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        return _status_payload(db, t)


@router.post("/extension-installed")
def extension_installed(token: str = Query(...),
                        session_id: Optional[str] = Query(default=None)):
    """Manual fallback for Screen 3 — operator clicks "I've installed it".
    Advances the stage to 'clients'. No payment gate: the tenant is already in a
    live trial from the moment they finished signup."""
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        require_not_demo(t)
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
        require_not_demo(t)
        if not t.active:
            raise HTTPException(402, "Payment not complete")

        for ci in clients:
            name = ci.name.strip()
            dupe = db.execute(
                select(Client).where(Client.tenant_id == t.id, Client.name == name)
            ).scalar_one_or_none()
            if dupe:
                raise HTTPException(409, f"A client named '{name}' already exists")
            # The client's default fuel seeds manually-entered arrays below and
            # is stored on the Client so autopop arrays (created later by
            # /v1/sync) inherit the operator's onboarding fuel choice.
            client_fuel = normalize_fuel(ci.default_fuel_type)
            c = Client(
                tenant_id=t.id,
                name=name,
                contact_email=ci.contact_email,
                gmp_email=(ci.gmp_email.lower().strip() if ci.gmp_email else None),
                gmp_username=(ci.gmp_username.strip() if ci.gmp_username and ci.gmp_username.strip() else None),
                gmp_autopopulate=bool(ci.gmp_autopopulate),
                default_fuel_type=client_fuel,
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
                    # Per-array fuel wins; otherwise inherit the client default.
                    fuel_type=normalize_fuel(ai.fuel_type, client_fuel),
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
        # Use the canonical billable count (not soft-deleted, not excluded) so
        # the reconciled Stripe quantity matches the dashboard estimate.
        subscription_id = t.stripe_subscription_id
        tenant_id = t.id
        tenant_email = t.contact_email
        array_count = billable_array_count(db, t.id)

    # Now that every client + array is persisted, bring Stripe in line with the
    # real array count. This runs SYNCHRONOUSLY before we return (W2-11) so the
    # Done screen's reconciled count + monthly total are accurate the moment the
    # SPA advances. Best-effort — never blocks the operator reaching Screen 5.
    reconcile_subscription_quantity(
        subscription_id, array_count, tenant_id, tenant_email)

    return {"ok": True, "client_ids": created_ids, "array_count": array_count}


# ─── 5. complete ─────────────────────────────────────────────────────────

class CompleteBody(BaseModel):
    # Optional password the operator chose on /onboarding/info. Set here at
    # complete-time (rather than via a separate /v1/auth/set-password call
    # after the dashboard loads) so the session_token we mint below isn't
    # the operator's first chance to authenticate from another device —
    # they can password-login immediately on their phone the moment they
    # finish onboarding. Magic-link stays as the always-available fallback.
    password: Optional[str] = None


@router.post("/complete")
def complete(background_tasks: BackgroundTasks, token: str = Query(...), body: Optional[CompleteBody] = None):
    """Finish onboarding: mark stage='done', send the deferred welcome email,
    and fire a magic-link sign-in email so the operator can reach the
    dashboard (reuses account.py's auth flow)."""
    with SessionLocal() as db:
        t = _tenant_by_token(db, token)
        require_not_demo(t)
        if not t.active:
            raise HTTPException(402, "Payment not complete")
        # Set the operator's password BEFORE we mark stage=done. If the
        # password is malformed we want a 400 here, not silently dropping
        # it and shipping the operator to the dashboard with no password.
        # _validate_password_strength raises HTTPException(400, ...).
        if body and body.password:
            _validate_password_strength(body.password)
            t.password_hash = _hash_password(body.password)
        t.onboarding_stage = "done"
        db.commit()
        email = t.contact_email
        name = t.operator_name or t.company_name or t.name
        tenant_key = t.tenant_key
        plan = t.plan
        tenant_id = t.id
        trial_ends_at = t.trial_ends_at
        product = t.product

    # Deferred welcome email (NOT sent by the webhook for onboarding-flow tenants).
    # NEPOOL-only: this email is about the Chrome extension + activation code +
    # utility-portal capture — none of which exist for an Array Operator owner.
    # AO's single welcome is the product-aware trial-welcome email below.
    if (product or "nepool") != "array_operator":
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
    # or device (the session token above only lives in this browser). Scope to
    # the product just onboarded so a dual-product email gets THIS account's
    # link, not whichever happens to be newest.
    magic_link_email_sent = issue_magic_link(email, product=product)

    # Second email: a sample report so they see what their clients will receive.
    # Best-effort — already swallows its own exceptions, never blocks completion.
    # NEPOOL-only: the sample is a quarterly NEPOOL-GIS *client* workbook, which
    # an Array Operator owner has no use for (no clients, no quarterly filings).
    sample_email_sent = False
    if (product or "nepool") != "array_operator":
        sample_email_sent = send_sample_workbook_email(
            to=email, name=name, dashboard_url=f"{branding.dashboard_url(product)}")

    # Third email: trial welcome — explains the 14-day trial and primes them
    # to add clients/arrays. trial_ends_at is set by the Stripe webhook;
    # fall back to now+14 if it hasn't landed yet.
    # Non-critical to the response, so queue the trial-welcome email and the
    # internal alert as background work — keeps completion fast under a signup
    # burst (the magic link above stays synchronous since its status is returned).
    # trial_ends_at falls back to now+14 if the Stripe webhook lagged.
    try:
        from datetime import datetime as _dt, timedelta as _td
        _trial_end = trial_ends_at if trial_ends_at else _dt.utcnow() + _td(days=14)
        _trial_end_str = _trial_end.strftime("%B %-d, %Y")
        background_tasks.add_task(
            send_trial_welcome_email,
            to=email, name=name,
            trial_end_iso_date=_trial_end_str,
            dashboard_url=f"{branding.dashboard_url(product)}",
            product=product,
        )
    except Exception as e:
        logger.warning("Could not queue trial welcome email for %s: %s", email, e)

    background_tasks.add_task(
        send_internal_alert,
        "🌞 Onboarding complete",
        f"Tenant {tenant_id} ({email}) finished the onboarding wizard.",
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
    require_not_demo(t)
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


# ─── Public "request a utility" (pre-signup, no auth) ────────────────────────

class PublicUtilityRequest(BaseModel):
    """Home-page "don't see your utility?" submission. Public — no session.

    Length-capped to blunt abuse since this is unauthenticated. The optional
    `email` lets us follow up with an anonymous prospect; `willing_to_help`
    flags a volunteer offering to share a portal login so we can build the
    adapter (a strong lead — surfaced loudly in the internal alert)."""
    utility_name: str = Field(..., min_length=2, max_length=120)
    region: Optional[str] = Field(None, max_length=80)
    email: Optional[EmailStr] = None
    notes: Optional[str] = Field(None, max_length=600)
    willing_to_help: bool = False


@router.post("/request-utility")
def public_request_utility(body: PublicUtilityRequest, request: Request):
    """Prospect-submitted utility-addition request from the public home page.

    Same routing as the authenticated /v1/account/request-utility (emails Ford,
    fires the Hermes add-a-utility webhook when configured), but requires no
    login — a prospect can ask for their utility before signing up. The
    `willing_to_help` box lets them volunteer to help expand coverage.
    """
    from .utility_request import submit_utility_request

    # Unauthenticated + fans out to email + the Hermes webhook — throttle per-IP
    # so it can't be used to flood Ford's inbox or the add-a-utility webhook.
    from . import ratelimit
    ratelimit.enforce(request, "public_request_utility_ip", max_hits=8, window_s=600,
                      message="Too many requests from your network — please try again in a few minutes.")

    name = (body.utility_name or "").strip()
    if not name:
        raise HTTPException(422, "Utility name is required")

    return submit_utility_request(
        tenant_id="(public-home-page)",
        tenant_name="Prospect (not signed up)",
        tenant_email=body.email,
        utility_name=name,
        portal_url=None,
        region=body.region,
        notes=body.notes,
        willing_to_help=bool(body.willing_to_help),
    )
