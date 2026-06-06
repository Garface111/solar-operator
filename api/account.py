"""
Solar Operator — customer self-serve portal API.

Magic-link auth. Browser flow:
  1. User visits /account.html, enters email.
  2. Front-end POSTs /v1/auth/request { email }.
  3. We email a one-time link: /account.html?token=<64-char hex>
  4. Front-end POSTs /v1/auth/verify { token } → returns short session_token
     (a JWT-shaped opaque string we just sign with itsdangerous-style HMAC).
  5. Browser stashes session_token in localStorage, includes as
     `Authorization: Bearer <session_token>` on subsequent /v1/account/* calls.

Tokens expire after 15 minutes (login link) or 30 days (session).
"""
from __future__ import annotations

import calendar
import os
import re
import secrets
import logging
import hmac
import hashlib
import base64
import json
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
import time
from datetime import date, datetime, timedelta
from typing import Optional

import stripe
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, func, or_

from .db import SessionLocal
from .models import Tenant, Client, Array, Bill, LoginToken, UtilityAccount, DeleteHistory, ClientMergeDismissal, ArrayMergeDismissal, now
from .notify import _send_via_resend, send_internal_alert, FROM_ADDRESS
from .providers import PROVIDERS, PROVIDER_CODES, get_provider
from .stripe_helpers import reconcile_subscription_quantity
from .email_templates import (
    DEFAULT_SUBJECT_TEMPLATE, DEFAULT_BODY_TEMPLATE, DEFAULT_SIGNOFF,
    DEFAULT_DASHBOARD_URL, MERGE_TAGS, build_context, render_email,
    render_merge, resolve_from_header,
)

logger = logging.getLogger(__name__)

APP_URL = os.getenv("APP_URL", "https://solaroperator.org").rstrip("/")
# Public, buyer-facing dashboard URL. Netlify 200-proxies solaroperator.org/accounts
# to the FastAPI mount at /app/* on Railway, so magic-link emails and Stripe
# return URLs use the clean marketing-domain path, never the raw Railway host.
PUBLIC_DASHBOARD_URL = os.getenv("PUBLIC_DASHBOARD_URL", f"{APP_URL}/accounts").rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")  # if blank, generated at startup
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
LOGIN_LINK_TTL_SECONDS = 15 * 60  # 15 minutes

# Per-array monthly price for the dashboard billing summary. Sourced from the
# live Stripe price (STRIPE_ARRAY_PRICE_ID) when configured, falling back to the
# same $15 default used by the onboarding checkout (ONBOARDING_ARRAY_CENTS).
STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")
ARRAY_PRICE_CENTS = int(os.getenv("ONBOARDING_ARRAY_CENTS", "1500"))  # $15/array/mo
_array_price_cache: dict = {}


def _array_price_cents() -> tuple[int, str]:
    """Monthly per-array price in (cents, currency).

    Prefers the live Stripe price referenced by STRIPE_ARRAY_PRICE_ID and caches
    the result for the process lifetime; falls back to the hardcoded $15 default
    when Stripe is unconfigured or unreachable. Only successful lookups (and the
    stable no-Stripe fallback) are cached, so a transient Stripe error doesn't
    pin us to the fallback forever."""
    if _array_price_cache:
        return _array_price_cache["cents"], _array_price_cache["currency"]
    cents, currency, cacheable = ARRAY_PRICE_CENTS, "usd", True
    if STRIPE_ARRAY_PRICE_ID and os.getenv("STRIPE_SECRET_KEY"):
        cacheable = False
        try:
            price = stripe.Price.retrieve(STRIPE_ARRAY_PRICE_ID)
            if price.get("unit_amount") is not None:
                cents = int(price["unit_amount"])
            if price.get("currency"):
                currency = price["currency"]
            cacheable = True
        except Exception as e:  # noqa: BLE001 — billing display must never 500
            logger.warning("billing-summary: Stripe price retrieve failed: %s", e)
    if cacheable:
        _array_price_cache["cents"] = cents
        _array_price_cache["currency"] = currency
    return cents, currency

# Fallback: derive a stable secret from DATABASE_URL so it survives restarts
# but is unique per environment. Set SESSION_SECRET explicitly in prod for
# real rotation control.
if not SESSION_SECRET:
    seed = os.getenv("DATABASE_URL", "") or "fallback-dev-secret"
    SESSION_SECRET = hashlib.sha256(seed.encode()).hexdigest()


router = APIRouter()


# ─── session token signing (compact HMAC, no JWT lib needed) ─────────────

def _sign_session(tenant_id: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    payload = {"tid": tenant_id, "exp": int(time.time()) + ttl_seconds}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    sig = hmac.new(SESSION_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body.decode() + "." + sig


def mint_session_for_tenant(tenant_id: str) -> str:
    """Mint a fresh ~30-day dashboard session token bound to `tenant_id`.

    Same opaque format and TTL as the session handed out by /v1/auth/verify
    after a magic-link exchange. Lets the onboarding /complete flow log the
    operator straight into the dashboard without a round-trip through their
    email inbox."""
    return _sign_session(tenant_id)


def _verify_session(token: str) -> Optional[str]:
    """Return tenant_id if valid, None otherwise."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload.get("tid")


def tenant_from_session(authorization: Optional[str]) -> Tenant:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue")
    token = authorization.split(" ", 1)[1].strip()
    tenant_id = _verify_session(token)
    if not tenant_id:
        raise HTTPException(401, "Session expired — sign in again")
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            raise HTTPException(404, "Account not found")
        # NOTE: we DO let inactive (canceled) tenants reach /account so they
        # can see their status and (if comped) export their data. Read-only
        # gated downstream where needed.
        return t


# ─── schemas ─────────────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    email: EmailStr
    persist: bool = True  # if True, mint 30-day session; if False, mint 1-day session


class AuthVerify(BaseModel):
    token: str


class UpdateEmail(BaseModel):
    email: EmailStr


class UpdateFrequency(BaseModel):
    frequency: str  # weekly | monthly | quarterly


class UpdateCcOnReports(BaseModel):
    cc_on_reports: bool


# ─── Client (sub-client) CRUD ───────────────────────────────────────────

class ClientCreate(BaseModel):
    name: str
    contact_email: Optional[EmailStr] = None
    cc_emails: Optional[str] = None  # comma-separated
    report_frequency: Optional[str] = None  # null → coerced to "quarterly" on write
    notes: Optional[str] = None
    # GMP auto-populate (mirrors onboarding Screen 4 — editable post-onboarding).
    # The operator logs into GMP with either an email or a username; we match on
    # whichever is set when the extension captures a session.
    gmp_email: Optional[EmailStr] = None
    gmp_username: Optional[str] = None
    gmp_autopopulate: Optional[bool] = None
    # VEC auto-populate (mirrors GMP triple for the VEC provider).
    vec_email: Optional[EmailStr] = None
    vec_username: Optional[str] = None
    vec_autopopulate: Optional[bool] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    cc_emails: Optional[str] = None
    report_frequency: Optional[str] = None
    active: Optional[bool] = None
    notes: Optional[str] = None
    gmp_email: Optional[EmailStr] = None
    gmp_username: Optional[str] = None
    gmp_autopopulate: Optional[bool] = None
    vec_email: Optional[EmailStr] = None
    vec_username: Optional[str] = None
    vec_autopopulate: Optional[bool] = None


def _client_to_dict(c: Client, array_count: int = 0) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "contact_email": c.contact_email,
        "cc_emails": c.cc_emails,
        "report_frequency": c.report_frequency,
        "active": c.active,
        "array_count": array_count,
        "last_delivery_at": c.last_delivery_at.isoformat() if c.last_delivery_at else None,
        "notes": c.notes,
        "gmp_email": c.gmp_email,
        "gmp_username": c.gmp_username,
        "gmp_autopopulate": c.gmp_autopopulate,
        "gmp_last_sync_at": c.gmp_last_sync_at.isoformat() if c.gmp_last_sync_at else None,
        "vec_email": c.vec_email,
        "vec_username": c.vec_username,
        "vec_autopopulate": c.vec_autopopulate,
        "vec_last_sync_at": c.vec_last_sync_at.isoformat() if c.vec_last_sync_at else None,
        "last_delivered_at": c.last_delivered_at.isoformat() if c.last_delivered_at else None,
        "last_bounced_at": c.last_bounced_at.isoformat() if c.last_bounced_at else None,
        "last_bounce_reason": c.last_bounce_reason,
        "is_placeholder": c.is_placeholder,
    }


# ─── Array CRUD (under a client) ────────────────────────────────────────

class ArrayAccountInput(BaseModel):
    provider: str  # one of providers.PROVIDER_CODES
    account_number: str
    nickname: Optional[str] = None


class ArrayCreate(BaseModel):
    name: str
    nepool_gis_id: Optional[str] = None
    region: Optional[str] = None
    bill_offset_months: Optional[int] = 1
    notes: Optional[str] = None
    accounts: Optional[list[ArrayAccountInput]] = None
    # optional list of utility logins / account numbers powering this array


class ArrayUpdate(BaseModel):
    name: Optional[str] = None
    nepool_gis_id: Optional[str] = None
    region: Optional[str] = None
    bill_offset_months: Optional[int] = None
    notes: Optional[str] = None
    excluded: Optional[bool] = None


def _array_to_dict(a: Array, accounts: list[UtilityAccount]) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "nepool_gis_id": a.nepool_gis_id,
        "region": a.region,
        "bill_offset_months": a.bill_offset_months,
        "notes": a.notes,
        "excluded": bool(a.excluded),
        "accounts": [
            {
                "id": ac.id,
                "provider": ac.provider,
                "provider_label": (get_provider(ac.provider) or {}).get("label", ac.provider),
                "account_number": ac.account_number,
                "nickname": ac.nickname,
            }
            for ac in accounts
        ],
    }


# ─── magic-link auth ────────────────────────────────────────────────────

def issue_magic_link(email: str, persist: bool = True) -> bool:
    """Create a single-use login token for `email` and email the sign-in link.

    Returns True if a matching tenant existed and an email was attempted,
    False if no tenant matched (caller should NOT leak that distinction to
    untrusted clients). Shared by /v1/auth/request and the onboarding
    /v1/onboarding/complete flow.
    """
    email = email.lower().strip()
    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.contact_email == email)
        ).scalars().first()
        if not t:
            return False

        token = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(seconds=LOGIN_LINK_TTL_SECONDS)
        db.add(LoginToken(token=token, tenant_id=t.id, email=email, expires_at=expires, persist_session=persist))
        db.commit()
        tenant_name = t.name

    # Magic link lands on the dashboard SPA, which exchanges this one-time login
    # token for a session via POST /v1/auth/verify (see web/app AuthGate).
    link = f"{PUBLIC_DASHBOARD_URL}/?token={token}"
    html = f"""\
<!DOCTYPE html><html><body style="margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f4;padding:30px 0;color:#1a2a1f;">
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" width="520" style="max-width:520px;background:white;border-radius:12px;overflow:hidden;">
<tr><td style="background:#2e6b3a;padding:24px 32px;color:white;">
  <div style="font-size:20px;font-weight:700;">Solar Operator</div>
  <div style="font-size:13px;color:#cfe4d3;margin-top:4px;">Sign-in link for {tenant_name or 'your account'}</div>
</td></tr>
<tr><td style="padding:32px;font-size:15px;line-height:1.6;">
<p>Click the button below to sign in to your Solar Operator account:</p>
<p style="text-align:center;margin:28px 0;">
  <a href="{link}" style="background:#2e6b3a;color:white;padding:13px 28px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-block;">Sign in to my account</a>
</p>
<p style="font-size:13px;color:#667;">This link expires in 15 minutes and can only be used once. If you didn't request it, you can ignore this email — no one can sign in without it.</p>
<p style="font-size:12px;color:#aaa;word-break:break-all;margin-top:24px;">Or paste this link into your browser:<br>{link}</p>
</td></tr>
</table>
</td></tr></table></body></html>
"""
    text = f"Sign in to Solar Operator: {link}\n\nLink expires in 15 minutes."
    sent = _send_via_resend(
        to=email,
        subject="Sign in to Solar Operator",
        html=html,
        text=text,
    )
    if not sent:
        logger.error(
            "magic_link_send_failed: email=%s reason=%s",
            email,
            getattr(_send_via_resend, "_last_error", "unknown"),
        )
    return True


@router.post("/v1/auth/request")
def auth_request(req: AuthRequest):
    """Email a one-time login link to a known customer. Always returns OK
    (don't leak which emails are registered)."""
    issue_magic_link(req.email, persist=req.persist)
    return {"ok": True, "delivered": True}


@router.post("/v1/auth/verify")
def auth_verify(req: AuthVerify):
    """Exchange a single-use login token for a session token."""
    token = req.token.strip()
    with SessionLocal() as db:
        row = db.execute(
            select(LoginToken).where(LoginToken.token == token)
        ).scalars().first()
        if not row:
            raise HTTPException(401, "Invalid or expired sign-in link")
        if row.used_at is not None:
            raise HTTPException(401, "This sign-in link was already used")
        if row.expires_at < datetime.utcnow():
            raise HTTPException(401, "Sign-in link expired — request a new one")
        row.used_at = datetime.utcnow()
        tenant_id = row.tenant_id
        persist = row.persist_session if row.persist_session is not None else True
        db.commit()

    ttl = SESSION_TTL_SECONDS if persist else 24 * 3600  # 30 days or 1 day
    session_token = _sign_session(tenant_id, ttl_seconds=ttl)
    return {"ok": True, "session_token": session_token, "expires_in": ttl}


# ─── password auth ──────────────────────────────────────────────────────

class SetPasswordBody(BaseModel):
    password: str
    current_password: Optional[str] = None  # required when an existing password is set


class PasswordLoginBody(BaseModel):
    email: EmailStr
    password: str


def _validate_password_strength(pw: str) -> None:
    if len(pw) < 10:
        raise HTTPException(400, "Password must be at least 10 characters")
    if not re.search(r"[a-zA-Z]", pw):
        raise HTTPException(400, "Password must contain at least one letter")
    if not re.search(r"[0-9]", pw):
        raise HTTPException(400, "Password must contain at least one digit")


def _hash_password(pw: str) -> str:
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)
    return ctx.hash(pw)


def _verify_password(pw: str, hashed: str) -> bool:
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return ctx.verify(pw, hashed)


@router.post("/v1/auth/password-login")
def password_login(body: PasswordLoginBody):
    """Exchange email + password for a session token.

    Returns 401 with a generic message on any failure to prevent email enumeration.
    Mints the same session token shape as /v1/auth/verify so downstream is identical."""
    email = body.email.lower().strip()
    _GENERIC_ERROR = "Invalid email or password"
    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.contact_email == email)
        ).scalars().first()
        if not t or not t.password_hash:
            raise HTTPException(401, _GENERIC_ERROR)
        if not _verify_password(body.password, t.password_hash):
            raise HTTPException(401, _GENERIC_ERROR)
        tenant_id = t.id

    session_token = _sign_session(tenant_id)
    logger.info("password_login_success: email=%s tenant=%s", email, tenant_id)
    return {"ok": True, "session_token": session_token, "expires_in": SESSION_TTL_SECONDS}


@router.post("/v1/auth/set-password")
def set_password(body: SetPasswordBody,
                 authorization: Optional[str] = Header(default=None)):
    """Set or change the operator's password.

    First time (has_password=false): current_password is not required — the
    existing session (from magic-link) is proof of identity.
    Changing (has_password=true): current_password must be provided and correct.
    Password rules: min 10 chars, at least 1 letter, at least 1 digit."""
    t = tenant_from_session(authorization)
    _validate_password_strength(body.password)

    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        if t.password_hash is not None:
            # Changing an existing password — require the current one
            if not body.current_password:
                raise HTTPException(400, "Current password is required to change your password")
            if not _verify_password(body.current_password, t.password_hash):
                raise HTTPException(400, "Current password is incorrect")
        t.password_hash = _hash_password(body.password)
        db.commit()

    return {"ok": True, "has_password": True}


# ─── account read ───────────────────────────────────────────────────────

@router.get("/v1/account")
def account_me(authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        from .models import UtilityAccount, UtilitySession, Bill
        # Re-read inside this session for fresh relationships
        t = db.get(Tenant, t.id)
        accounts_count = db.execute(
            select(func.count()).select_from(UtilityAccount)
            .where(UtilityAccount.tenant_id == t.id)
        ).scalar() or 0
        last_sess = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == t.id)
            .order_by(UtilitySession.captured_at.desc())
        ).scalars().first()
        bills_count = db.execute(
            select(func.count()).select_from(Bill).where(Bill.tenant_id == t.id)
        ).scalar() or 0
        clients_count = db.execute(
            select(func.count()).select_from(Client).where(
                Client.tenant_id == t.id,
                Client.deleted_at.is_(None),
            )
        ).scalar() or 0
        return {
            "tenant_id": t.id,
            # Activation code the customer pastes into the Chrome extension.
            # Safe to return to the authenticated owner of this tenant.
            "tenant_key": t.tenant_key,
            "name": t.name,
            "email": t.contact_email,
            "plan": t.plan,
            "active": t.active,
            "subscription_status": t.subscription_status,
            "report_frequency": t.report_frequency,
            "cc_on_reports": bool(t.cc_on_reports),
            # V2 email customization: current values (null = using default) plus
            # the built-in defaults so the dashboard can show them as placeholders.
            "send_from_email": t.send_from_email,
            "send_from_name": t.send_from_name,
            "email_subject_template": t.email_subject_template,
            "email_body_template": t.email_body_template,
            "send_mode": t.send_mode or "to_client",
            "valid_send_modes": list(_VALID_SEND_MODES),
            "valid_frequencies": ["monthly", "quarterly"],
            "default_email_subject": DEFAULT_SUBJECT_TEMPLATE,
            "default_email_body": DEFAULT_BODY_TEMPLATE,
            "merge_tags": list(MERGE_TAGS),
            "last_pull_at": t.last_pull_at.isoformat() if t.last_pull_at else None,
            "last_delivery_at": t.last_delivery_at.isoformat() if t.last_delivery_at else None,
            "extension_heartbeat_at": t.extension_heartbeat_at.isoformat() if t.extension_heartbeat_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "trial_ends_at": t.trial_ends_at.isoformat() if t.trial_ends_at else None,
            "has_password": bool(t.password_hash),
            "accounts_count": int(accounts_count),
            "bills_count": int(bills_count),
            "clients_count": int(clients_count),
            "session": {
                "captured_at": last_sess.captured_at.isoformat() if last_sess else None,
                "expires_at": last_sess.expires_at.isoformat() if last_sess and last_sess.expires_at else None,
                "last_refresh_at": last_sess.last_refresh_at.isoformat() if last_sess and last_sess.last_refresh_at else None,
                "refresh_failures": last_sess.refresh_failures if last_sess else 0,
            } if last_sess else None,
        }


# ─── account mutations ──────────────────────────────────────────────────

@router.post("/v1/account/email")
def update_email(body: UpdateEmail, authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    new_email = body.email.lower().strip()
    with SessionLocal() as db:
        # Refuse if email is taken by another active tenant
        clash = db.execute(
            select(Tenant).where(
                Tenant.contact_email == new_email,
                Tenant.id != t.id,
            )
        ).scalars().first()
        if clash:
            raise HTTPException(409, "That email is already in use on another account")
        t = db.get(Tenant, t.id)
        t.contact_email = new_email
        db.commit()

    # Mirror change to Stripe so receipts go to the right address
    if t.stripe_customer_id:
        try:
            stripe.Customer.modify(t.stripe_customer_id, email=new_email)
        except Exception as e:
            logger.warning("Failed to sync email to Stripe: %s", e)

    return {"ok": True, "email": new_email}


@router.post("/v1/account/regen-key")
def regen_activation_key(authorization: Optional[str] = Header(default=None)):
    """Generate a new tenant_key (activation code), invalidating the old one.

    The old code stops working immediately. The operator must paste the new
    code into the extension options page to resume captures."""
    t = tenant_from_session(authorization)
    new_key = "sol_live_" + secrets.token_urlsafe(32)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        t.tenant_key = new_key
        db.commit()
    return {"ok": True, "tenant_key": new_key}


@router.post("/v1/account/frequency")
def update_frequency(body: UpdateFrequency, authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    if body.frequency not in ("monthly", "quarterly"):
        raise HTTPException(400, "frequency must be monthly or quarterly")
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        t.report_frequency = body.frequency
        db.commit()
    return {"ok": True, "frequency": body.frequency}


@router.post("/v1/account/cc-on-reports")
def update_cc_on_reports(body: UpdateCcOnReports,
                         authorization: Optional[str] = Header(default=None)):
    """Toggle 'send me a copy of every report'. Returns the updated value."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        t.cc_on_reports = bool(body.cc_on_reports)
        db.commit()
        value = t.cc_on_reports
    return {"ok": True, "cc_on_reports": value}


# ─── V2 email customization ─────────────────────────────────────────────

# Fake client name for the live preview. Quarter/period data is computed
# dynamically from the real most-recently-complete quarter so the preview
# matches what actual reports will say at send time.
_PREVIEW_CLIENT = "Sample Client"
_VALID_SEND_MODES = ("to_client", "to_me", "to_both")


class EmailSettings(BaseModel):
    """All optional. A field left out (None) is untouched; an empty/blank
    string clears that field back to the built-in default. send_mode, when
    provided, must be one of to_client | to_me."""
    send_from_email: Optional[str] = None
    send_from_name: Optional[str] = None
    email_subject_template: Optional[str] = None
    email_body_template: Optional[str] = None
    send_mode: Optional[str] = None


def _blank_to_none(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip()
    return s or None


def _validate_email_settings(body: EmailSettings) -> None:
    if body.send_from_email is not None:
        e = body.send_from_email.strip()
        if e and ("@" not in e or " " in e or "." not in e.split("@")[-1]):
            raise HTTPException(400, "send_from_email must be a valid email address")
    if body.send_mode is not None and body.send_mode.strip():
        if body.send_mode.strip() not in _VALID_SEND_MODES:
            raise HTTPException(400, "send_mode must be 'to_client', 'to_me', or 'to_both'")


def _effective(req_val: Optional[str], stored_val: Optional[str]) -> Optional[str]:
    """For preview: use the request value when the field was provided (incl.
    explicit blank = clear), else fall back to what's stored on the tenant."""
    if req_val is None:
        return stored_val
    return _blank_to_none(req_val)


@router.get("/v1/account/from-domain-status")
def from_domain_status(authorization: Optional[str] = Header(default=None)):
    """Return the Resend verification status for the tenant's custom send_from_email
    domain. Returns {"domain": ..., "status": "verified"|"pending"|"unverified"|"none"}.

    "none" means no custom address is set (platform default in use).
    "unverified" means the domain exists in Resend but DNS isn't confirmed.
    "pending" means the domain was added but verification hasn't resolved yet.
    "verified" means Resend has confirmed the domain and custom-From will work.

    On any Resend API error, returns {"domain": ..., "status": "unknown"} so the
    UI can show a neutral state rather than a hard error."""
    t = tenant_from_session(authorization)
    email = (t.send_from_email or "").strip().lower()
    if not email or "@" not in email:
        return {"domain": None, "status": "none"}

    domain = email.split("@", 1)[1]
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        return {"domain": domain, "status": "unknown"}

    try:
        import resend as resend_sdk
        resend_sdk.api_key = resend_key
        domains = resend_sdk.Domains.list()
        # SDK returns a list of domain objects; find one matching our domain.
        for d in (domains.get("data") or []):
            if (d.get("name") or "").lower() == domain:
                status = (d.get("status") or "").lower()
                if status == "verified":
                    return {"domain": domain, "status": "verified"}
                return {"domain": domain, "status": status or "pending"}
        # Domain not found in this Resend account — can't verify custom From.
        return {"domain": domain, "status": "unverified"}
    except Exception as exc:
        logger.warning("from-domain-status: Resend API error: %s", exc)
        return {"domain": domain, "status": "unknown"}


@router.post("/v1/account/email-settings")
def update_email_settings(body: EmailSettings,
                          authorization: Optional[str] = Header(default=None)):
    """Persist the tenant's report-email customization. Returns the updated
    settings. Empty-string fields clear back to the built-in default; omitted
    fields are left unchanged."""
    t = tenant_from_session(authorization)
    _validate_email_settings(body)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        if body.send_from_email is not None:
            v = _blank_to_none(body.send_from_email)
            t.send_from_email = v.lower() if v else None
        if body.send_from_name is not None:
            t.send_from_name = _blank_to_none(body.send_from_name)
        if body.email_subject_template is not None:
            t.email_subject_template = _blank_to_none(body.email_subject_template)
        if body.email_body_template is not None:
            t.email_body_template = _blank_to_none(body.email_body_template)
        if body.send_mode is not None and body.send_mode.strip():
            t.send_mode = body.send_mode.strip()
        db.commit()
        return {
            "ok": True,
            "send_from_email": t.send_from_email,
            "send_from_name": t.send_from_name,
            "email_subject_template": t.email_subject_template,
            "email_body_template": t.email_body_template,
            "send_mode": t.send_mode or "to_client",
        }


@router.post("/v1/account/email-preview")
def preview_email(body: EmailSettings,
                  authorization: Optional[str] = Header(default=None)):
    """Render a sample report email using the supplied settings layered over
    the tenant's stored defaults. Uses a fake client so the tenant can eyeball
    exactly what goes out before committing."""
    t = tenant_from_session(authorization)
    _validate_email_settings(body)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        tenant_name = t.name or "your company"
        tenant_email = (t.contact_email or "").strip()
        eff_from_email = _effective(body.send_from_email, t.send_from_email)
        eff_from_name = _effective(body.send_from_name, t.send_from_name)
        eff_subject = _effective(body.email_subject_template, t.email_subject_template)
        eff_body = _effective(body.email_body_template, t.email_body_template)
        eff_mode = ((body.send_mode if body.send_mode is not None else t.send_mode)
                    or "to_client").strip() or "to_client"

    ctx = build_context(
        client_name=_PREVIEW_CLIENT, tenant_name=tenant_name,
        arrays_count=3, tenant_email=tenant_email,
        dashboard_url=DEFAULT_DASHBOARD_URL,
    )
    subject, html, text = render_email(
        subject_template=eff_subject, body_template=eff_body, ctx=ctx)
    from_header = resolve_from_header(eff_from_email, eff_from_name, tenant_name)
    if eff_mode == "to_me":
        recipient = tenant_email or "you (your account email)"
    elif eff_mode == "to_both":
        client_part = f"{_PREVIEW_CLIENT} (your client's contact email)"
        you_part = tenant_email or "you (your account email)"
        recipient = f"{client_part} + {you_part}"
    else:
        recipient = f"{_PREVIEW_CLIENT} (your client's contact email)"
    return {
        "ok": True,
        "subject": subject,
        "html": html,
        "text": text,
        "from": from_header or FROM_ADDRESS,
        "to": recipient,
        "send_mode": eff_mode,
    }


class _SendModeBody(BaseModel):
    send_mode: str


@router.post("/v1/account/reports/send-mode")
def patch_reports_send_mode(body: _SendModeBody,
                             authorization: Optional[str] = Header(default=None)):
    """Quick-save the recipient-routing default (from the NextRunCard toggle)."""
    mode = (body.send_mode or "").strip()
    if mode not in _VALID_SEND_MODES:
        raise HTTPException(400, "send_mode must be 'to_client', 'to_me', or 'to_both'")
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        t.send_mode = mode
        db.commit()
        return {"ok": True, "send_mode": t.send_mode}


class SendReportBody(BaseModel):
    """Optional body for /v1/account/send-report. When client_ids is provided,
    only those clients (validated against the tenant) get the report — used by
    the dashboard's per-client checkbox picker. When omitted, ALL active
    clients get it (legacy behavior). send_mode, when provided, is saved as
    the tenant default before delivery so the modal slider takes immediate effect."""
    client_ids: Optional[list[int]] = None
    send_mode: Optional[str] = None


@router.post("/v1/account/send-report")
def send_my_report(
    body: Optional[SendReportBody] = None,
    authorization: Optional[str] = Header(default=None),
):
    """Customer-triggered: 'send my latest reports now.'

    Without a body or with client_ids=None: fans out to every active client.
    With client_ids=[1,2,3]: fans out only to those clients (must belong to
    the calling tenant). Returns the same {ok, client_count, delivered,
    results} shape either way so the frontend can use one code path."""
    t = tenant_from_session(authorization)
    if not t.active and t.subscription_status not in ("active", "trialing", "comped"):
        raise HTTPException(402, "Reactivate your subscription to send reports")

    # If the modal slider included a send_mode, save it as the new default first.
    # This covers the race where the toggle PATCH might not have completed yet.
    if body and body.send_mode:
        mode = body.send_mode.strip()
        if mode in _VALID_SEND_MODES:
            with SessionLocal() as db:
                t_db = db.get(Tenant, t.id)
                if t_db:
                    t_db.send_mode = mode
                    db.commit()

    ids = (body.client_ids if body else None) or []
    # Defer heavy import (avoid circulars at module load)
    from .delivery import deliver_for_tenant, deliver_for_client

    if not ids:
        # Legacy path — every active client
        return deliver_for_tenant(t.id, override_to=None, triggered_by="self-serve")

    # Per-client picker path — validate ownership, then fan out
    with SessionLocal() as db:
        rows = db.execute(
            select(Client)
            .where(Client.tenant_id == t.id, Client.id.in_(ids),
                   Client.active == True)  # noqa: E712
        ).scalars().all()
        if not rows:
            raise HTTPException(404, "None of the selected clients belong to your account.")
        resolved_ids = [c.id for c in rows]
    results = []
    delivered = 0
    for cid in resolved_ids:
        r = deliver_for_client(cid, triggered_by="self-serve-picker")
        results.append(r)
        if r.get("ok") and r.get("email_sent"):
            delivered += 1
    return {
        "ok": True,
        "client_count": len(resolved_ids),
        "delivered": delivered,
        "results": results,
    }


@router.post("/v1/account/send-sample-report")
def send_sample_report(authorization: Optional[str] = Header(default=None)):
    """Send a sample workbook to the logged-in operator's own email only.

    Picks the first active client with at least one array, builds a real
    workbook for that client, and emails it to the tenant's own contact_email
    with '[SAMPLE]' prepended to the subject. No client is ever contacted.
    Useful for operators who want to see exactly what their clients will receive
    before the first real quarterly run."""
    t = tenant_from_session(authorization)
    if not t.active and t.subscription_status not in ("active", "trialing", "comped"):
        raise HTTPException(402, "Reactivate your subscription to send sample reports")
    tenant_email = (t.contact_email or "").strip()
    if not tenant_email:
        raise HTTPException(422, "Add an email address to your account settings first.")
    with SessionLocal() as db:
        row = db.execute(
            select(Client)
            .join(Array, Array.client_id == Client.id)
            .where(Client.tenant_id == t.id, Client.active == True)  # noqa: E712
            .order_by(Client.name.asc())
            .limit(1)
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(
                422,
                "Add a client and at least one array first — then come back to preview the email.",
            )
        client_id = row.id
    from .delivery import deliver_for_client
    result = deliver_for_client(
        client_id, override_to=tenant_email, triggered_by="sample",
        subject_prefix="[SAMPLE] ",
    )
    if not result.get("ok"):
        raise HTTPException(500,
            result.get("reason") or "Sample workbook generation failed — check server logs.")
    if not result.get("email_sent"):
        raise HTTPException(502,
            "Sample workbook built but email delivery failed — check your Resend configuration.")
    return {**result, "sample": True, "sent_to": tenant_email}


# ─── Email template studio (V2, June 2026) ──────────────────────────────────
# Operators can customize the per-client report email template through a
# full-screen AI-assisted studio. The tenant's overrides live in
# Tenant.email_subject_template / email_body_template; null means "use the
# built-in default" (see api/email_templates.py).

def _query_sample_client_ctx(db, tenant_id: str, tenant_name: str,
                             tenant_email: str,
                             signoff_template: Optional[str] = None) -> tuple[dict, str]:
    """Return (merge_ctx, sample_client_name) using first client with email or fallback."""
    client = db.execute(
        select(Client)
        .where(
            Client.tenant_id == tenant_id,
            Client.active == True,  # noqa: E712
            Client.contact_email.is_not(None),
            Client.deleted_at.is_(None),
        )
        .order_by(Client.name.asc())
        .limit(1)
    ).scalars().first()
    if client:
        n_arrays = db.execute(
            select(func.count()).select_from(Array)
            .where(Array.client_id == client.id, Array.deleted_at.is_(None))
        ).scalar() or 1
        client_name = client.name
    else:
        n_arrays = 3
        client_name = "Sample Client"
    ctx = build_context(
        client_name=client_name,
        tenant_name=tenant_name,
        arrays_count=n_arrays,
        tenant_email=tenant_email,
        dashboard_url=DEFAULT_DASHBOARD_URL,
        signoff_template=signoff_template,
    )
    return ctx, client_name


class _TemplateBody(BaseModel):
    subject_template: Optional[str] = None
    body_template: Optional[str] = None
    signoff: Optional[str] = None


@router.get("/v1/account/reports/email-template")
def get_email_template(authorization: Optional[str] = Header(default=None)):
    """Return the tenant's current email template with resolved defaults."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        sample_client = db.execute(
            select(Client)
            .where(
                Client.tenant_id == t.id,
                Client.active == True,  # noqa: E712
                Client.contact_email.is_not(None),
                Client.deleted_at.is_(None),
            )
            .order_by(Client.name.asc())
            .limit(1)
        ).scalars().first()
        has_client_email = sample_client is not None
        resolved_subject = t.email_subject_template or DEFAULT_SUBJECT_TEMPLATE
        resolved_body = t.email_body_template or DEFAULT_BODY_TEMPLATE
        resolved_signoff = t.email_signoff or DEFAULT_SIGNOFF
        return {
            "subject_template": resolved_subject,
            "body_template": resolved_body,
            "signoff": resolved_signoff,
            "is_default_subject": t.email_subject_template is None,
            "is_default_body": t.email_body_template is None,
            "is_default_signoff": t.email_signoff is None,
            # Legacy field — kept for backward-compat with any callers that check it.
            "is_default": (t.email_subject_template is None
                           and t.email_body_template is None
                           and t.email_signoff is None),
            "from_email": t.contact_email,
            "available_tokens": list(MERGE_TAGS) + ["signoff"],
            "has_client_with_email": has_client_email,
            "sample_client_email": sample_client.contact_email if sample_client else None,
        }


@router.post("/v1/account/reports/email-template/preview")
def preview_email_template(body: _TemplateBody,
                           authorization: Optional[str] = Header(default=None)):
    """Render the proposed template with real sample data (first client with email)."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        tenant_name = t.name or "Your Company"
        tenant_email = (t.contact_email or "").strip()
        stored_subject = t.email_subject_template
        stored_body = t.email_body_template
        stored_signoff = t.email_signoff
        # Resolve signoff: request body overrides stored, stored overrides default.
        signoff_t = (body.signoff or "").strip() or stored_signoff or DEFAULT_SIGNOFF
        ctx, sample_client = _query_sample_client_ctx(
            db, t.id, tenant_name, tenant_email, signoff_template=signoff_t)
    subj_t = (body.subject_template or "").strip() or stored_subject or DEFAULT_SUBJECT_TEMPLATE
    body_t = (body.body_template or "").strip() or stored_body or DEFAULT_BODY_TEMPLATE
    return {
        "subject_rendered": render_merge(subj_t, ctx),
        "body_rendered": render_merge(body_t, ctx),
        "sample_client": sample_client,
    }


class _ChatBody(BaseModel):
    messages: list[dict]
    current_body: str
    current_subject: Optional[str] = None


@router.post("/v1/account/reports/email-template/chat")
def chat_email_template(body: _ChatBody,
                        authorization: Optional[str] = Header(default=None)):
    """Call the LLM to regenerate the template body/subject based on the conversation."""
    import os as _os
    from .email_templates import regenerate_template_via_ai
    tenant_from_session(authorization)
    api_key = _os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(503, "AI assistant not configured — set ANTHROPIC_API_KEY")
    recent_messages = body.messages[-10:]  # cap at 5 turns (10 messages) to bound tokens
    for m in recent_messages:
        if m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            raise HTTPException(400, "Each message must have role 'user'|'assistant' and string content")
    current_subject = (body.current_subject or "").strip() or DEFAULT_SUBJECT_TEMPLATE
    try:
        result = regenerate_template_via_ai(
            current_body=body.current_body,
            current_subject=current_subject,
            messages=recent_messages,
            api_key=api_key,
        )
    except Exception as exc:
        logger.exception("Template AI regen failed")
        raise HTTPException(502, f"AI request failed: {exc}") from exc
    return {
        "assistant_reply": result["reply"],
        "proposed_body": result["body"],
        "proposed_subject": result["subject"],
    }


@router.put("/v1/account/reports/email-template")
def save_email_template(body: _TemplateBody,
                        authorization: Optional[str] = Header(default=None)):
    """Persist the operator's custom template as their send-time default."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        if body.subject_template is not None:
            t.email_subject_template = _blank_to_none(body.subject_template)
        if body.body_template is not None:
            t.email_body_template = _blank_to_none(body.body_template)
        db.commit()
        return {
            "ok": True,
            "subject_template": t.email_subject_template,
            "body_template": t.email_body_template,
        }


@router.post("/v1/account/reports/email-template/test-send")
def test_send_email_template(body: _TemplateBody,
                             authorization: Optional[str] = Header(default=None)):
    """Render the proposed template with real data and send a [TEST] to the tenant's email."""
    t = tenant_from_session(authorization)
    tenant_email = (t.contact_email or "").strip()
    if not tenant_email:
        raise HTTPException(422, "Add an email address to your account first.")
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        tenant_name = t.name or "Your Company"
        from_header = resolve_from_header(
            t.send_from_email or t.contact_email,
            t.send_from_name,
            t.name,
        )
        stored_subject = t.email_subject_template
        stored_body = t.email_body_template
        stored_signoff = t.email_signoff
        signoff_t = (body.signoff or "").strip() or stored_signoff or DEFAULT_SIGNOFF
        ctx, _ = _query_sample_client_ctx(
            db, t.id, tenant_name, tenant_email, signoff_template=signoff_t)
    subj_t = (body.subject_template or "").strip() or stored_subject or DEFAULT_SUBJECT_TEMPLATE
    body_t = (body.body_template or "").strip() or stored_body or DEFAULT_BODY_TEMPLATE
    subject, html, text = render_email(
        subject_template=subj_t, body_template=body_t, ctx=ctx)
    from .notify import _send_via_resend
    sent = _send_via_resend(
        to=tenant_email,
        subject=f"[TEST] {subject}",
        html=html,
        text=text,
        from_addr=from_header,
    )
    if not sent:
        raise HTTPException(502, "Email delivery failed — check your Resend configuration.")
    return {"ok": True, "sent_to": tenant_email}


@router.post("/v1/account/reports/email-template/reset")
def reset_email_template(authorization: Optional[str] = Header(default=None)):
    """Clear the tenant's template overrides — reverts all three to the system built-in defaults."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        t.email_subject_template = None
        t.email_body_template = None
        t.email_signoff = None
        db.commit()
    return {"ok": True}


class _SignoffBody(BaseModel):
    signoff: Optional[str] = None


@router.put("/v1/account/reports/email-template/signoff")
def save_email_signoff(body: _SignoffBody,
                       authorization: Optional[str] = Header(default=None)):
    """Persist the operator's custom sign-off. Pass signoff=null to revert to default."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, t.id)
        t.email_signoff = _blank_to_none(body.signoff or "")
        db.commit()
        return {"ok": True, "signoff": t.email_signoff}


@router.get("/v1/account/billing-summary")
def billing_summary(authorization: Optional[str] = Header(default=None)):
    """What the tenant is actually billed for: the array count that drives the
    Stripe per-array quantity (every Array row under the tenant — the same count
    api/onboarding._reconcile_subscription_quantity reconciles), times the
    per-array price. Lets the operator verify their own invoice on the Account
    tab instead of inferring it from utility-account / bill counts that don't
    drive billing."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        billable = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
            )
        ).scalar() or 0
    cents, currency = _array_price_cents()
    billable = int(billable)
    return {
        "billable_arrays": billable,
        "price_cents": cents,
        "total_cents": billable * cents,
        "currency": currency,
    }


@router.get("/v1/account/next-invoice")
def next_invoice(authorization: Optional[str] = Header(default=None)):
    """Return the next Stripe invoice's amount and due date.

    Calls stripe.Invoice.upcoming() and surfaces the total and period_end so
    the dashboard billing strip can show 'Next charge: $X on <date>'. Returns
    null fields when Stripe is unconfigured, the tenant has no customer, or the
    API call fails — the billing strip just hides the next-charge line."""
    t = tenant_from_session(authorization)
    if not t.stripe_customer_id or not os.getenv("STRIPE_SECRET_KEY"):
        return {"amount_cents": None, "currency": None, "period_end": None}
    try:
        invoice = stripe.Invoice.upcoming(customer=t.stripe_customer_id)
        amount = invoice.get("amount_due") or invoice.get("amount_remaining")
        currency = invoice.get("currency")
        period_end = invoice.get("period_end")  # Unix timestamp
        from datetime import timezone
        pe_iso = (
            datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat()
            if period_end else None
        )
        return {"amount_cents": amount, "currency": currency, "period_end": pe_iso}
    except stripe.error.InvalidRequestError as e:
        # "No upcoming invoices" is not an error worth logging
        if "No upcoming invoices" in str(e):
            return {"amount_cents": None, "currency": None, "period_end": None}
        logger.warning("next-invoice: Stripe error for tenant %s: %s", t.id, e)
        return {"amount_cents": None, "currency": None, "period_end": None}
    except Exception as e:
        logger.warning("next-invoice: unexpected error for tenant %s: %s", t.id, e)
        return {"amount_cents": None, "currency": None, "period_end": None}


@router.get("/v1/account/billing-portal")
def billing_portal(authorization: Optional[str] = Header(default=None)):
    """Return a Stripe Billing Portal URL the customer can use to update card,
    download invoices, or cancel."""
    t = tenant_from_session(authorization)
    if not t.stripe_customer_id:
        raise HTTPException(404, "No Stripe customer linked — contact admin@solaroperator.org")
    if not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(500, "Stripe not configured")
    try:
        session = stripe.billing_portal.Session.create(
            customer=t.stripe_customer_id,
            return_url=f"{PUBLIC_DASHBOARD_URL}/",
        )
        return {"url": session.url}
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe error: {e}")


# ─── Clients (sub-clients) ──────────────────────────────────────────────

@router.get("/v1/account/clients")
def list_clients(authorization: Optional[str] = Header(default=None)):
    """List all sub-clients under the calling tenant."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        clients = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.deleted_at.is_(None),
            ).order_by(Client.name.asc())
        ).scalars().all()
        # Fetch all array counts in a single query rather than one per client.
        array_counts_rows = db.execute(
            select(Array.client_id, func.count(Array.id).label("n"))
            .where(
                Array.client_id.in_([c.id for c in clients]),
                Array.deleted_at.is_(None),
            )
            .group_by(Array.client_id)
        ).all()
        counts = {row.client_id: row.n for row in array_counts_rows}
        out = [_client_to_dict(c, array_count=counts.get(c.id, 0)) for c in clients]
    return {"ok": True, "clients": out}


@router.post("/v1/account/clients")
def create_client(body: ClientCreate,
                  authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    if body.report_frequency and body.report_frequency not in (
            "monthly", "quarterly"):
        raise HTTPException(400,
            "report_frequency must be monthly or quarterly")
    with SessionLocal() as db:
        # Name dedup
        existing = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.name == name,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "A client with that name already exists")

        # Login dedup — if the operator's already added a client (manually
        # OR via portal autopop) with the same GMP/VEC email or username,
        # don't silently create a duplicate human. The conflict response
        # carries the existing client's id+name so the UI can offer
        # "open existing" instead of creating a dupe.
        gmp_email_norm = body.gmp_email.lower().strip() if body.gmp_email else None
        gmp_user_norm = (
            body.gmp_username.strip().lower() if body.gmp_username and body.gmp_username.strip() else None
        )
        vec_email_norm = body.vec_email.lower().strip() if body.vec_email else None
        vec_user_norm = (
            body.vec_username.strip().lower() if body.vec_username and body.vec_username.strip() else None
        )
        login_conflicts = []
        if gmp_email_norm:
            login_conflicts.append(func.lower(Client.gmp_email) == gmp_email_norm)
        if gmp_user_norm:
            login_conflicts.append(func.lower(Client.gmp_username) == gmp_user_norm)
        if vec_email_norm:
            login_conflicts.append(func.lower(Client.vec_email) == vec_email_norm)
        if vec_user_norm:
            login_conflicts.append(func.lower(Client.vec_username) == vec_user_norm)
        if login_conflicts:
            dup = db.execute(
                select(Client).where(
                    Client.tenant_id == t.id,
                    Client.deleted_at.is_(None),
                    or_(*login_conflicts),
                ).order_by(Client.id).limit(1)
            ).scalar_one_or_none()
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "login-already-claimed",
                        "message": (
                            f"That utility login is already on \"{dup.name}\". "
                            "Open that client instead of adding a duplicate."
                        ),
                        "existing_client_id": dup.id,
                        "existing_client_name": dup.name,
                    },
                )

        c = Client(
            tenant_id=t.id, name=name,
            contact_email=body.contact_email,
            cc_emails=body.cc_emails,
            report_frequency=body.report_frequency or "quarterly",
            notes=body.notes,
            gmp_email=(body.gmp_email.lower().strip() if body.gmp_email else None),
            gmp_username=(body.gmp_username.strip()
                          if body.gmp_username and body.gmp_username.strip() else None),
            gmp_autopopulate=bool(body.gmp_autopopulate),
            vec_email=(body.vec_email.lower().strip() if body.vec_email else None),
            vec_username=(body.vec_username.strip()
                          if body.vec_username and body.vec_username.strip() else None),
            vec_autopopulate=bool(body.vec_autopopulate),
            active=True,
        )
        db.add(c); db.commit(); db.refresh(c)
        return {"ok": True, "client": _client_to_dict(c, 0)}


# ── Merge-suggestion + merge endpoints ──────────────────────────────────
# Cross-provider dup detection: a single human signed in via GMP under
# bruce@example.com and via VEC under bgenereaux — backend can't dedup
# automatically (no field overlap) but the data has signals: shared
# contact_email, normalized name match, overlapping NEPOOL IDs on
# arrays. We score those signals, surface the top match per client, and
# let the operator merge with one click. "Keep separate" dismissals are
# remembered in client_merge_dismissals so we don't nag.


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, drop common
    business suffixes so 'Bruce Genereaux LLC' and 'bruce genereaux'
    compare equal."""
    import re
    s = (name or "").lower()
    s = re.sub(r"[,.\-_/]", " ", s)
    s = re.sub(
        r"\b(llc|inc|incorporated|llp|lp|corp|corporation|co|company|trust)\b",
        " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _last_name_token(normalized: str) -> str:
    """Heuristic last-name extraction: last whitespace-delimited token
    of the normalized name. 'bruce genereaux' → 'genereaux'."""
    parts = normalized.split()
    return parts[-1] if parts else ""


def _merge_candidates_for(db, tenant_id: str, target: Client) -> list[dict]:
    """Score plausible merge candidates for `target` within `tenant_id`.
    Returns at most 3, sorted by score desc. Skips dismissed pairs."""
    others = db.execute(
        select(Client).where(
            Client.tenant_id == tenant_id,
            Client.deleted_at.is_(None),
            Client.id != target.id,
        )
    ).scalars().all()

    if not others:
        return []

    # Look up dismissed pairs once
    dismissed_pairs: set[tuple[int, int]] = set()
    for d in db.execute(
        select(ClientMergeDismissal).where(
            ClientMergeDismissal.tenant_id == tenant_id,
        )
    ).scalars().all():
        dismissed_pairs.add((d.client_a_id, d.client_b_id))

    # NEPOOL IDs by client (for shared-array signal). Bills are tied
    # to UtilityAccounts → Arrays → Client.
    nepool_by_client: dict[int, set[str]] = {}
    for arr in db.execute(
        select(Array).where(
            Array.tenant_id == tenant_id,
            Array.nepool_gis_id.is_not(None),
        )
    ).scalars().all():
        if arr.client_id is not None:
            nepool_by_client.setdefault(arr.client_id, set()).add(
                str(arr.nepool_gis_id))

    target_nepool = nepool_by_client.get(target.id, set())
    target_norm = _normalize_name(target.name)
    target_last = _last_name_token(target_norm)
    target_contact = (target.contact_email or "").lower().strip()

    candidates: list[tuple[int, dict]] = []
    for other in others:
        # Dismissed?
        a, b = sorted([target.id, other.id])
        if (a, b) in dismissed_pairs:
            continue

        score = 0
        reasons: list[str] = []

        # Contact email match — very strong
        other_contact = (other.contact_email or "").lower().strip()
        if target_contact and other_contact and target_contact == other_contact:
            score += 60
            reasons.append("same contact email")

        # Name signals
        other_norm = _normalize_name(other.name)
        if target_norm and other_norm:
            if target_norm == other_norm:
                score += 50
                reasons.append("same name")
            else:
                other_last = _last_name_token(other_norm)
                if target_last and other_last and target_last == other_last and len(target_last) >= 3:
                    score += 25
                    reasons.append(f"shared last name “{target_last}”")

        # Shared NEPOOL-GIS array — same physical site
        other_nepool = nepool_by_client.get(other.id, set())
        shared = target_nepool & other_nepool
        if shared:
            score += 40 * min(len(shared), 2)
            sample = next(iter(shared))
            reasons.append(f"shared NEPOOL-GIS array {sample}")

        # Cross-provider login complement — one has GMP, the other VEC.
        # Weak on its own but a small nudge so an isolated name match
        # ranks above an isolated contact-email match across the same
        # provider (which would already have been hard-blocked at
        # create-time).
        t_has_gmp = bool(target.gmp_email or target.gmp_username)
        t_has_vec = bool(target.vec_email or target.vec_username)
        o_has_gmp = bool(other.gmp_email or other.gmp_username)
        o_has_vec = bool(other.vec_email or other.vec_username)
        cross_provider = (t_has_gmp and o_has_vec and not t_has_vec and not o_has_gmp) or \
                         (t_has_vec and o_has_gmp and not t_has_gmp and not o_has_vec)
        if cross_provider and score > 0:
            score += 10
            reasons.append("cross-provider logins")

        if score >= 30:
            candidates.append((score, {
                "id": other.id,
                "name": other.name,
                "score": score,
                "reasons": reasons,
                "has_gmp": o_has_gmp,
                "has_vec": o_has_vec,
            }))

    candidates.sort(key=lambda t: t[0], reverse=True)
    return [c[1] for c in candidates[:3]]


@router.get("/v1/account/clients/{client_id}/merge-suggestions")
def get_merge_suggestions(client_id: int,
                          authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        client = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.id == client_id,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not client:
            raise HTTPException(404, "client not found")
        return {"ok": True, "suggestions": _merge_candidates_for(db, t.id, client)}


class MergeIntoBody(BaseModel):
    dst_client_id: int


@router.post("/v1/account/clients/{src_client_id}/merge-into")
def merge_client_into(src_client_id: int, body: MergeIntoBody,
                      authorization: Optional[str] = Header(default=None)):
    """Merge `src_client_id` INTO `body.dst_client_id`. Reparents arrays
    + utility accounts, merges login fields (dst keeps its own if set,
    otherwise inherits from src), then soft-deletes src.

    Login-conflict rule: if BOTH clients have a non-null value for the
    same login field (e.g. both have a gmp_email), we keep dst's value
    and discard src's. This is intentional — operator confirmation
    happens at the UI layer; this endpoint trusts the choice.

    Idempotent on already-deleted src (returns 200 with no-op flag)."""
    t = tenant_from_session(authorization)
    if src_client_id == body.dst_client_id:
        raise HTTPException(400, "src and dst must differ")

    now_ts = datetime.utcnow()
    with SessionLocal() as db:
        src = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.id == src_client_id,
            )
        ).scalar_one_or_none()
        dst = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.id == body.dst_client_id,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not src or not dst:
            raise HTTPException(404, "client not found")
        if src.deleted_at is not None:
            return {"ok": True, "noop": True, "dst_client_id": dst.id}

        # Snapshot pre-merge state for undo (before any mutations)
        src_array_ids_snapshot = [
            arr.id for arr in db.execute(
                select(Array).where(
                    Array.tenant_id == t.id,
                    Array.client_id == src.id,
                    Array.deleted_at.is_(None),
                )
            ).scalars().all()
        ]
        dst_before_merge = {
            "contact_email": dst.contact_email,
            "gmp_email": dst.gmp_email,
            "gmp_username": dst.gmp_username,
            "vec_email": dst.vec_email,
            "vec_username": dst.vec_username,
            "notes": dst.notes,
            "gmp_autopopulate": dst.gmp_autopopulate,
            "vec_autopopulate": dst.vec_autopopulate,
        }
        src_name = src.name

        # Reparent arrays
        for arr in db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.client_id == src.id,
            )
        ).scalars().all():
            arr.client_id = dst.id

        # Reparent utility_accounts — handled via Array's client_id
        # above (UtilityAccount.array_id), but VEC bills/usage_raw may
        # be keyed differently. UtilityAccount table has tenant_id, not
        # client_id, so no per-account reparent needed.

        # Merge login fields — dst wins on conflict
        for field in ("contact_email", "gmp_email", "gmp_username",
                      "vec_email", "vec_username", "notes"):
            if getattr(dst, field) in (None, "") and getattr(src, field):
                setattr(dst, field, getattr(src, field))

        # Always preserve autopop flags as True if either side had them
        if src.gmp_autopopulate or dst.gmp_autopopulate:
            dst.gmp_autopopulate = True
        if src.vec_autopopulate or dst.vec_autopopulate:
            dst.vec_autopopulate = True

        # Soft-delete src
        src.deleted_at = now_ts

        # Clear any dismissal entries involving src (they're irrelevant now)
        pair_a, pair_b = sorted([src.id, dst.id])
        db.query(ClientMergeDismissal).filter(
            ClientMergeDismissal.tenant_id == t.id,
            or_(
                ClientMergeDismissal.client_a_id == src.id,
                ClientMergeDismissal.client_b_id == src.id,
            ),
        ).delete(synchronize_session=False)

        # Persist undo snapshot in DeleteHistory (1-hour TTL).
        undo_token = secrets.token_hex(8)
        db.add(DeleteHistory(
            tenant_id=t.id,
            undo_token=undo_token,
            payload={
                "kind": "merge_undo",
                "src_client_id": src_client_id,
                "src_client_name": src_name,
                "dst_client_id": body.dst_client_id,
                "src_array_ids": src_array_ids_snapshot,
                "dst_before_merge": dst_before_merge,
                "clients": [src_client_id],
                "arrays": [],
                "utility_accounts": [],
            },
            expires_at=now_ts + timedelta(hours=1),
        ))

        db.commit()
        db.refresh(dst)
        # Re-count arrays for the response
        n_arrays = db.execute(
            select(func.count()).select_from(Array).where(
                Array.client_id == dst.id
            )
        ).scalar() or 0
        return {
            "ok": True,
            "dst_client": _client_to_dict(dst, array_count=n_arrays),
            "merged_from_id": src_client_id,
            "merged_client_id": src_client_id,
            "undo_token": undo_token,
        }


class MergeUndoBody(BaseModel):
    undo_token: str


@router.post("/v1/account/clients/merge-undo")
def undo_merge(body: MergeUndoBody,
               authorization: Optional[str] = Header(default=None)):
    """Reverse a merge within the 1-hour undo window.

    Restores the soft-deleted source client, re-assigns its original arrays
    back to it, and reverts the destination client's login fields to their
    pre-merge state. Returns 410 if the window has elapsed."""
    t = tenant_from_session(authorization)
    now_ts = datetime.utcnow()
    with SessionLocal() as db:
        history = db.execute(
            select(DeleteHistory).where(
                DeleteHistory.undo_token == body.undo_token,
                DeleteHistory.tenant_id == t.id,
            )
        ).scalar_one_or_none()
        if not history:
            raise HTTPException(404, "Undo token not found")
        if history.consumed_at is not None:
            raise HTTPException(409, "This undo token has already been used")
        if history.expires_at < now_ts:
            raise HTTPException(410, "Undo window expired — the 1-hour undo period has passed")
        payload = history.payload or {}
        if payload.get("kind") != "merge_undo":
            raise HTTPException(400, "Token is not a merge undo token")

        src_id = payload["src_client_id"]
        src_array_ids = payload.get("src_array_ids", [])
        dst_id = payload["dst_client_id"]
        dst_before = payload.get("dst_before_merge", {})

        # Restore source client
        src = db.get(Client, src_id)
        if not src or src.tenant_id != t.id:
            raise HTTPException(404, "Source client not found")
        src.deleted_at = None

        # Re-assign arrays back to source
        if src_array_ids:
            db.execute(
                Array.__table__.update()
                .where(Array.id.in_(src_array_ids), Array.tenant_id == t.id)
                .values(client_id=src_id)
            )

        # Restore destination client's pre-merge fields
        dst = db.get(Client, dst_id)
        if dst and dst.tenant_id == t.id:
            for field, val in dst_before.items():
                if hasattr(dst, field):
                    setattr(dst, field, val)

        history.consumed_at = now_ts
        db.commit()
        return {"ok": True, "restored_client_id": src_id}


@router.post("/v1/account/clients/{client_id}/dismiss-merge/{other_id}")
def dismiss_merge_suggestion(client_id: int, other_id: int,
                             authorization: Optional[str] = Header(default=None)):
    """Operator clicked 'Keep separate' on a suggested pair. Persist so
    we don't suggest the same merge again. Symmetric — pair is stored
    as (min_id, max_id)."""
    t = tenant_from_session(authorization)
    if client_id == other_id:
        raise HTTPException(400, "ids must differ")
    a, b = sorted([client_id, other_id])
    with SessionLocal() as db:
        # Both must still exist + belong to this tenant
        n = db.execute(
            select(func.count()).select_from(Client).where(
                Client.tenant_id == t.id,
                Client.id.in_([a, b]),
                Client.deleted_at.is_(None),
            )
        ).scalar() or 0
        if n != 2:
            raise HTTPException(404, "one or both clients not found")
        existing = db.execute(
            select(ClientMergeDismissal).where(
                ClientMergeDismissal.tenant_id == t.id,
                ClientMergeDismissal.client_a_id == a,
                ClientMergeDismissal.client_b_id == b,
            )
        ).scalar_one_or_none()
        if not existing:
            db.add(ClientMergeDismissal(
                tenant_id=t.id, client_a_id=a, client_b_id=b,
            ))
            db.commit()
        return {"ok": True}


# ── Array merge-suggestion + merge endpoints ────────────────────────────
# Same pattern as the client variant, scoped to arrays. Common scenario:
# an auto-created array from a fresh capture (named after the GMP account
# number) duplicates an operator-imported array with the same NEPOOL ID
# and a friendly name like "Tannery Brook". Operator should merge with
# one click instead of deleting one side and losing the linked UAs.


def _normalize_array_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Lighter touch
    than the client normalization — array names don't carry business
    suffixes, they're just site names or account numbers."""
    import re
    s = (name or "").lower()
    s = re.sub(r"[,.\-_/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _array_merge_candidates_for(db, tenant_id: str, target: Array) -> list[dict]:
    """Score plausible array merge candidates within tenant. Same shape
    as the client variant but with array-specific signals:
        same NEPOOL-GIS ID         +80  (very strong — it's an asset ID)
        same normalized name       +50
        shared utility account     +60  (per shared account, capped)
        same client + close name   +20
    Returns top 3 with score >= 30. Skips dismissed pairs."""
    others = db.execute(
        select(Array).where(
            Array.tenant_id == tenant_id,
            Array.deleted_at.is_(None),
            Array.id != target.id,
        )
    ).scalars().all()
    if not others:
        return []

    dismissed_pairs: set[tuple[int, int]] = set()
    for d in db.execute(
        select(ArrayMergeDismissal).where(
            ArrayMergeDismissal.tenant_id == tenant_id,
        )
    ).scalars().all():
        dismissed_pairs.add((d.array_a_id, d.array_b_id))

    # UA account_numbers by array
    uas_by_array: dict[int, set[str]] = {}
    for ua in db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == tenant_id,
            UtilityAccount.deleted_at.is_(None),
            UtilityAccount.array_id.is_not(None),
        )
    ).scalars().all():
        uas_by_array.setdefault(ua.array_id, set()).add(ua.account_number)

    target_uas = uas_by_array.get(target.id, set())
    target_norm = _normalize_array_name(target.name)
    target_nepool = (target.nepool_gis_id or "").strip()

    candidates: list[tuple[int, dict]] = []
    for other in others:
        a, b = sorted([target.id, other.id])
        if (a, b) in dismissed_pairs:
            continue

        score = 0
        reasons: list[str] = []

        # NEPOOL match — strongest signal. NEPOOL IDs are assigned per
        # physical asset, so two arrays with the same one are almost
        # certainly the same array.
        other_nepool = (other.nepool_gis_id or "").strip()
        if target_nepool and other_nepool and target_nepool == other_nepool:
            score += 80
            reasons.append(f"same NEPOOL-GIS ID {target_nepool}")

        # Name match
        other_norm = _normalize_array_name(other.name)
        if target_norm and other_norm and target_norm == other_norm:
            score += 50
            reasons.append("same name")

        # Shared utility account
        other_uas = uas_by_array.get(other.id, set())
        shared = target_uas & other_uas
        if shared:
            sample = next(iter(shared))
            score += 60 * min(len(shared), 2)
            reasons.append(
                f"shared utility account {sample}" +
                (f" (+{len(shared) - 1} more)" if len(shared) > 1 else "")
            )

        # Same client + similar name (close, not exact)
        if (target.client_id is not None
                and target.client_id == other.client_id
                and target_norm and other_norm
                and target_norm != other_norm
                and (target_norm in other_norm or other_norm in target_norm)):
            score += 20
            reasons.append("same client, similar name")

        if score >= 30:
            candidates.append((score, {
                "id": other.id,
                "name": other.name,
                "score": score,
                "reasons": reasons,
                "client_id": other.client_id,
                "nepool_gis_id": other.nepool_gis_id,
            }))

    candidates.sort(key=lambda t: t[0], reverse=True)
    return [c[1] for c in candidates[:3]]


@router.get("/v1/account/arrays/{array_id}/merge-suggestions")
def get_array_merge_suggestions(array_id: int,
                                authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.id == array_id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not arr:
            raise HTTPException(404, "array not found")
        return {"ok": True, "suggestions": _array_merge_candidates_for(db, t.id, arr)}


class ArrayMergeIntoBody(BaseModel):
    dst_array_id: int


@router.post("/v1/account/arrays/{src_array_id}/merge-into")
def merge_array_into(src_array_id: int, body: ArrayMergeIntoBody,
                     authorization: Optional[str] = Header(default=None)):
    """Merge `src_array_id` INTO `body.dst_array_id`. Reparents utility
    accounts to dst, merges metadata (dst wins on conflict), soft-deletes
    src. Bills follow utility_accounts via account_id so no per-bill
    reparent is needed.

    Idempotent on already-soft-deleted src (returns noop:true)."""
    t = tenant_from_session(authorization)
    if src_array_id == body.dst_array_id:
        raise HTTPException(400, "src and dst must differ")

    with SessionLocal() as db:
        src = db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.id == src_array_id,
            )
        ).scalar_one_or_none()
        dst = db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.id == body.dst_array_id,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not src or not dst:
            raise HTTPException(404, "array not found")
        if src.deleted_at is not None:
            return {"ok": True, "noop": True, "dst_array_id": dst.id}

        # Reparent utility accounts to dst
        n_uas = db.execute(
            select(func.count()).select_from(UtilityAccount).where(
                UtilityAccount.tenant_id == t.id,
                UtilityAccount.array_id == src.id,
            )
        ).scalar() or 0
        for ua in db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == t.id,
                UtilityAccount.array_id == src.id,
            )
        ).scalars().all():
            ua.array_id = dst.id

        # Merge metadata: dst keeps its values when set, otherwise inherit
        for field in ("nepool_gis_id", "region", "first_connect_date",
                      "solar_adder_cents", "notes"):
            if getattr(dst, field) in (None, "") and getattr(src, field):
                setattr(dst, field, getattr(src, field))
        # bill_offset_months: dst wins (it's a per-array config the operator
        # may have set deliberately, e.g. Starlake's 0 vs default 1)

        # Excluded flag: AND semantics — only excluded if BOTH were
        if not (src.excluded and dst.excluded):
            dst.excluded = False

        # Soft-delete src
        src.deleted_at = now()

        # Clear dismissals involving src
        db.query(ArrayMergeDismissal).filter(
            ArrayMergeDismissal.tenant_id == t.id,
            or_(
                ArrayMergeDismissal.array_a_id == src.id,
                ArrayMergeDismissal.array_b_id == src.id,
            ),
        ).delete(synchronize_session=False)

        db.commit()
        db.refresh(dst)
        return {
            "ok": True,
            "merged_from_id": src.id,
            "dst_array": {
                "id": dst.id,
                "name": dst.name,
                "client_id": dst.client_id,
                "nepool_gis_id": dst.nepool_gis_id,
                "bill_offset_months": dst.bill_offset_months,
                "excluded": dst.excluded,
                "utility_accounts_count": (
                    db.execute(
                        select(func.count()).select_from(UtilityAccount).where(
                            UtilityAccount.array_id == dst.id,
                            UtilityAccount.deleted_at.is_(None),
                        )
                    ).scalar() or 0
                ),
            },
            "reparented_utility_accounts": n_uas,
        }


@router.post("/v1/account/arrays/{array_id}/dismiss-merge/{other_id}")
def dismiss_array_merge_suggestion(array_id: int, other_id: int,
                                   authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    if array_id == other_id:
        raise HTTPException(400, "ids must differ")
    a, b = sorted([array_id, other_id])
    with SessionLocal() as db:
        n = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id,
                Array.id.in_([a, b]),
                Array.deleted_at.is_(None),
            )
        ).scalar() or 0
        if n != 2:
            raise HTTPException(404, "one or both arrays not found")
        existing = db.execute(
            select(ArrayMergeDismissal).where(
                ArrayMergeDismissal.tenant_id == t.id,
                ArrayMergeDismissal.array_a_id == a,
                ArrayMergeDismissal.array_b_id == b,
            )
        ).scalar_one_or_none()
        if not existing:
            db.add(ArrayMergeDismissal(
                tenant_id=t.id, array_a_id=a, array_b_id=b,
            ))
            db.commit()
        return {"ok": True}


@router.patch("/v1/account/clients/{client_id}")
def update_client(client_id: int, body: ClientUpdate,
                  authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    if body.report_frequency and body.report_frequency not in (
            "monthly", "quarterly"):
        raise HTTPException(400,
            "report_frequency must be monthly or quarterly")
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
        if body.name is not None:
            new_name = body.name.strip()
            if new_name and new_name != c.name:
                clash = db.execute(
                    select(Client).where(
                        Client.tenant_id == t.id,
                        Client.name == new_name,
                        Client.id != c.id,
                    )
                ).scalar_one_or_none()
                if clash:
                    raise HTTPException(409,
                        "Another client already has that name")
                c.name = new_name
                # Stamp so re-captures know the operator curated this name.
                c.name_edited_at = now()
        # Use model_fields_set so fields absent from the request body are NOT
        # touched — the standard PATCH semantic. report_frequency=null coerces
        # to "quarterly" (inherit option removed Jun 6 2026).
        for field in ("contact_email", "cc_emails", "active", "notes",
                      "gmp_autopopulate", "vec_autopopulate"):
            if field in body.model_fields_set:
                setattr(c, field, getattr(body, field))
        if "report_frequency" in body.model_fields_set:
            c.report_frequency = body.report_frequency or "quarterly"
        if "gmp_email" in body.model_fields_set:
            c.gmp_email = (body.gmp_email or "").lower().strip() or None
        if "gmp_username" in body.model_fields_set:
            c.gmp_username = (body.gmp_username or "").strip() or None
        if "vec_email" in body.model_fields_set:
            c.vec_email = (body.vec_email or "").lower().strip() or None
        if "vec_username" in body.model_fields_set:
            c.vec_username = (body.vec_username or "").strip() or None
        # Any meaningful edit graduates the placeholder to a real client. The
        # walkthrough + dashboard prompts that depend on is_placeholder
        # vanish the moment the operator engages with the row.
        if c.is_placeholder:
            c.is_placeholder = False
        db.commit(); db.refresh(c)
        n_arr = db.execute(
            select(Array).where(Array.client_id == c.id)
        ).scalars().all()
        return {"ok": True, "client": _client_to_dict(c, len(n_arr))}


@router.delete("/v1/account/clients/{client_id}")
def delete_client(client_id: int,
                  authorization: Optional[str] = Header(default=None)):
    """Soft-delete a client and all its arrays + utility accounts.
    Returns an undo_token valid for 5 minutes."""
    t = tenant_from_session(authorization)
    now_ts = datetime.utcnow()
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
        if c.deleted_at is not None:
            raise HTTPException(404, "Client not found")

        arrays = db.execute(
            select(Array).where(Array.client_id == c.id, Array.deleted_at.is_(None))
        ).scalars().all()
        array_ids = [a.id for a in arrays]

        ua_ids: list[int] = []
        if array_ids:
            uas = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.array_id.in_(array_ids),
                    UtilityAccount.deleted_at.is_(None),
                )
            ).scalars().all()
            ua_ids = [u.id for u in uas]
            for u in uas:
                u.deleted_at = now_ts
        for a in arrays:
            a.deleted_at = now_ts
        c.deleted_at = now_ts

        undo_token = _make_undo_token(t.id, [client_id], now_ts)
        db.add(DeleteHistory(
            tenant_id=t.id,
            undo_token=undo_token,
            payload={"clients": [client_id], "arrays": array_ids, "utility_accounts": ua_ids},
            expires_at=now_ts + timedelta(minutes=5),
        ))
        db.commit()
        new_count = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id, Array.deleted_at.is_(None)
            )
        ).scalar() or 0
        sub_id = t.stripe_subscription_id
        tenant_email = t.contact_email

    reconcile_subscription_quantity(sub_id, int(new_count), t.id, tenant_email)
    return {"ok": True, "undo_token": undo_token}


@router.post("/v1/account/clients/{client_id}/refresh-capture")
def refresh_capture(client_id: int,
                    authorization: Optional[str] = Header(default=None)):
    """Re-read this client's GMP auto-populate freshness on demand.

    This does NOT poll GMP — captures arrive asynchronously via the extension's
    /v1/sync handler (which stamps Client.gmp_last_sync_at). The button just lets
    the operator pull the latest stored status without a full page reload, so the
    'Last GMP capture' indicator reflects any capture that has landed since the
    page opened. A real on-demand GMP poll is a separate feature."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
        n_arr = db.execute(
            select(Array).where(Array.client_id == c.id)
        ).scalars().all()
        return {"ok": True, "client": _client_to_dict(c, len(n_arr))}


@router.post("/v1/account/clients/{client_id}/send-report")
def send_one_client_report(client_id: int,
                           to: Optional[str] = Query(default=None),
                           authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
    if not t.active and t.subscription_status not in (
            "active", "trialing", "comped"):
        raise HTTPException(402, "Reactivate your subscription to send reports")
    override_to: Optional[str] = None
    if to is not None:
        tenant_email = (t.contact_email or "").strip().lower()
        if not tenant_email or to.strip().lower() != tenant_email:
            raise HTTPException(403, "?to must match your account email")
        override_to = to.strip()
    from .delivery import deliver_for_client
    return deliver_for_client(client_id, override_to=override_to,
                              triggered_by="self-serve")


@router.post("/v1/account/clients/{client_id}/resend-report")
def resend_client_report(client_id: int,
                         authorization: Optional[str] = Header(default=None)):
    """Re-send the current report for one client.

    Calls deliver_for_client and surfaces a clear result:
      200 {ok, recipient, client_name} on success.
      502 with the upstream error message when Resend fails — so the
          dashboard can show 'Couldn't resend — <reason>' to the operator."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
    if not t.active and t.subscription_status not in ("active", "trialing", "comped"):
        raise HTTPException(402, "Reactivate your subscription to send reports")

    from .delivery import deliver_for_client
    result = deliver_for_client(client_id, triggered_by="resend")

    if not result.get("ok"):
        reason = result.get("reason", "report generation failed")
        logger.error(
            "resend_report_failed: client_id=%s reason=%s", client_id, reason
        )
        raise HTTPException(502, reason)

    if not result.get("email_sent"):
        error_detail = (
            getattr(_send_via_resend, "_last_error", None)
            or "email delivery failed"
        )
        logger.error(
            "resend_email_failed: client_id=%s reason=%s", client_id, error_detail
        )
        raise HTTPException(502, f"Report generated but email failed: {error_detail}")

    return {
        "ok": True,
        "recipient": result.get("recipient", ""),
        "client_id": client_id,
        "client_name": result.get("client_name", ""),
    }


@router.get("/v1/account/clients/{client_id}/report.xlsx")
def download_client_report(
    client_id: int,
    quarter: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Stream a workbook for a client as a downloadable .xlsx attachment.

    If `quarter` is omitted, returns the current rolling-6-quarter workbook.
    If `quarter` is provided (e.g. 'Q1-2026'), the rolling window ends at that
    quarter so Q1-2026 is the most recent sheet."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
        client_name = c.name

    reference_date: Optional[date] = None
    if quarter:
        try:
            qy, qq = _parse_quarter_str(quarter)
            reference_date = _quarter_to_reference_date(qy, qq)
        except ValueError as e:
            raise HTTPException(400, str(e))

    from .writers import build_workbook
    tmpdir = tempfile.mkdtemp(prefix=f"so-dl-c{client_id}-")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", client_name)
    label = re.sub(r"[^A-Za-z0-9]", "-", quarter) if quarter else "latest"
    out_path = Path(tmpdir) / f"{safe_name}-{label}.xlsx"
    try:
        build_workbook(client_id=client_id, out_path=out_path, reference_date=reference_date)
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.exception(
            "download_client_report: build_workbook failed for client_id=%s", client_id)
        raise HTTPException(
            500,
            f"Couldn't build the report for {client_name}: {e}",
        )
    return FileResponse(
        str(out_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{safe_name}-{label}.xlsx",
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


# ─── Production data ────────────────────────────────────────────────────

@router.get("/v1/account/clients/{client_id}/production")
def client_production(
    client_id: int,
    months: int = Query(12, ge=1, le=36),
    authorization: Optional[str] = Header(default=None),
):
    """Monthly solar production totals for a client's arrays.

    Returns the last `months` unique production months, aggregated across all
    non-excluded, non-deleted arrays. Respects per-array bill_offset_months so
    same-month and prior-month billing schemes are handled correctly.

    Response shape:
      {ok, months: [{month, mwh, by_array: [{array_id, array_name, mwh}]}],
       stats: {last_30_days: {mwh, vs_prev_year_pct},
               last_12_months: {mwh, vs_prev_ttm_pct},
               ytd: {mwh}}}
    """
    from .models import Bill
    from collections import defaultdict

    t = tenant_from_session(authorization)

    _empty_stats = {
        "last_30_days": {"mwh": 0.0, "vs_prev_year_pct": None},
        "last_12_months": {"mwh": 0.0, "vs_prev_ttm_pct": None},
        "ytd": {"mwh": 0.0},
    }

    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")

        # account_id → (array_id, bill_offset_months, array_name)
        acct_rows = db.execute(
            select(
                UtilityAccount.id.label("acct_id"),
                Array.id.label("array_id"),
                Array.bill_offset_months,
                Array.name.label("array_name"),
            )
            .join(Array, UtilityAccount.array_id == Array.id)
            .where(
                Array.client_id == client_id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
                UtilityAccount.deleted_at.is_(None),
            )
        ).all()

        if not acct_rows:
            return {"ok": True, "months": [], "stats": _empty_stats}

        acct_to_array: dict[int, int] = {r.acct_id: r.array_id for r in acct_rows}
        array_offset: dict[int, int] = {r.array_id: r.bill_offset_months for r in acct_rows}
        array_names: dict[int, str] = {r.array_id: r.array_name for r in acct_rows}

        bills = db.execute(
            select(Bill).where(
                Bill.account_id.in_(list(acct_to_array.keys())),
                Bill.kwh_generated.isnot(None),
                Bill.period_end.isnot(None),
            )
        ).scalars().all()

        # monthly_data[(year, month)][array_id] += mwh
        monthly_data: dict[tuple[int, int], dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for bill in bills:
            aid = acct_to_array[bill.account_id]
            offset = array_offset[aid]
            pe = bill.period_end
            m, y = pe.month - offset, pe.year
            if m < 1:
                m += 12
                y -= 1
            monthly_data[(y, m)][aid] += (bill.kwh_generated or 0) / 1000.0

    all_months = sorted(monthly_data.keys())
    if not all_months:
        return {"ok": True, "months": [], "stats": _empty_stats}

    # Chart: last `months` production months, oldest→newest
    chart_months_yms = all_months[-months:]
    result_months = []
    for ym in chart_months_yms:
        arr_data = monthly_data[ym]
        total_mwh = sum(arr_data.values())
        by_array = sorted(
            [{"array_id": aid, "array_name": array_names[aid], "mwh": round(mwh, 3)}
             for aid, mwh in arr_data.items()],
            key=lambda x: x["array_name"],
        )
        result_months.append({
            "month": f"{ym[0]:04d}-{ym[1]:02d}",
            "mwh": round(total_mwh, 3),
            "by_array": by_array,
        })

    # Flat total per month for stats
    flat: dict[tuple[int, int], float] = {ym: sum(monthly_data[ym].values()) for ym in all_months}

    today = datetime.utcnow()
    cur_year, cur_month = today.year, today.month

    # Last 30 days = most recent production month we have data for
    last_ym = all_months[-1]
    last_30_mwh = flat[last_ym]
    prev_yr_ym = (last_ym[0] - 1, last_ym[1])
    prev_yr_mwh = flat.get(prev_yr_ym)
    if prev_yr_mwh is not None and prev_yr_mwh > 0:
        vs_prev_year_pct: float | None = round((last_30_mwh - prev_yr_mwh) / prev_yr_mwh * 100, 1)
    else:
        vs_prev_year_pct = None

    # Last 12 months vs prior TTM (requires ≥24 production months)
    ttm = all_months[-12:]
    ttm_mwh = sum(flat.get(ym, 0.0) for ym in ttm)
    prev_ttm = all_months[-24:-12] if len(all_months) >= 24 else []
    if len(prev_ttm) == 12:
        prev_ttm_mwh = sum(flat.get(ym, 0.0) for ym in prev_ttm)
        vs_prev_ttm_pct: float | None = (
            round((ttm_mwh - prev_ttm_mwh) / prev_ttm_mwh * 100, 1) if prev_ttm_mwh > 0 else None
        )
    else:
        vs_prev_ttm_pct = None

    # YTD: calendar year to current month
    ytd_mwh = sum(flat.get((cur_year, m), 0.0) for m in range(1, cur_month + 1))

    return {
        "ok": True,
        "months": result_months,
        "stats": {
            "last_30_days": {"mwh": round(last_30_mwh, 3), "vs_prev_year_pct": vs_prev_year_pct},
            "last_12_months": {"mwh": round(ttm_mwh, 1), "vs_prev_ttm_pct": vs_prev_ttm_pct},
            "ytd": {"mwh": round(ytd_mwh, 1)},
        },
    }


# ─── Utility provider catalog (UI dropdown source) ─────────────────────

@router.get("/v1/providers")
def list_providers():
    """List all utility data providers we support, with their scrape status.

    Public; no auth needed — the signup/onboarding flow shows this on the
    'connect your utility' page before the customer is logged in.
    """
    return {"ok": True, "providers": PROVIDERS}


# ─── Arrays under a client ──────────────────────────────────────────────

def _resolve_client_for_tenant(db, tenant_id: str, client_id: int) -> Client:
    c = db.get(Client, client_id)
    if not c or c.tenant_id != tenant_id:
        raise HTTPException(404, "Client not found")
    return c


@router.get("/v1/account/clients/{client_id}/arrays")
def list_client_arrays(client_id: int,
                       authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = _resolve_client_for_tenant(db, t.id, client_id)
        arrays = db.execute(
            select(Array).where(
                Array.client_id == c.id,
                Array.deleted_at.is_(None),
            ).order_by(Array.name.asc())
        ).scalars().all()
        array_ids = [a.id for a in arrays]
        # Fetch all utility accounts in one query rather than one per array.
        all_accts = db.execute(
            select(UtilityAccount)
            .where(
                UtilityAccount.array_id.in_(array_ids),
                UtilityAccount.deleted_at.is_(None),
            )
            .order_by(UtilityAccount.account_number.asc())
        ).scalars().all() if array_ids else []
        accts_by_array: dict[int, list] = {aid: [] for aid in array_ids}
        for acc in all_accts:
            accts_by_array.setdefault(acc.array_id, []).append(acc)
        out = [_array_to_dict(a, accts_by_array.get(a.id, [])) for a in arrays]
    return {"ok": True, "client_id": client_id, "arrays": out}


@router.post("/v1/account/clients/{client_id}/arrays")
def create_array(client_id: int, body: ArrayCreate,
                 authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    # validate providers up front so we don't partial-insert
    for acc in (body.accounts or []):
        code = (acc.provider or "").lower().strip()
        if code not in PROVIDER_CODES:
            raise HTTPException(400,
                f"Unknown provider '{acc.provider}'. "
                f"Use one of: {', '.join(sorted(PROVIDER_CODES))}")
        if not (acc.account_number or "").strip():
            raise HTTPException(400,
                "Each utility account needs an account_number")

    with SessionLocal() as db:
        c = _resolve_client_for_tenant(db, t.id, client_id)
        existing = db.execute(
            select(Array).where(Array.tenant_id == t.id, Array.name == name)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "An array with that name already exists")
        arr = Array(
            tenant_id=t.id, client_id=c.id, name=name,
            nepool_gis_id=body.nepool_gis_id,
            region=body.region,
            bill_offset_months=body.bill_offset_months
                if body.bill_offset_months is not None else 1,
            notes=body.notes,
        )
        db.add(arr); db.flush()
        # Add accounts (each is a sub-meter login)
        for acc in (body.accounts or []):
            db.add(UtilityAccount(
                tenant_id=t.id, array_id=arr.id,
                provider=acc.provider.lower().strip(),
                account_number=acc.account_number.strip(),
                nickname=(acc.nickname or arr.name).strip(),
            ))
        db.commit()
        accts = db.execute(
            select(UtilityAccount).where(UtilityAccount.array_id == arr.id)
        ).scalars().all()
        new_array_count = db.execute(
            select(func.count()).select_from(Array).where(Array.tenant_id == t.id)
        ).scalar() or 0
        sub_id = t.stripe_subscription_id
        tenant_email = t.contact_email
        result = {"ok": True, "array": _array_to_dict(arr, accts)}

    reconcile_subscription_quantity(sub_id, int(new_array_count), t.id, tenant_email)
    return result


@router.patch("/v1/account/clients/{client_id}/arrays/{array_id}")
def update_array(client_id: int, array_id: int, body: ArrayUpdate,
                 authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = _resolve_client_for_tenant(db, t.id, client_id)
        a = db.get(Array, array_id)
        if not a or a.tenant_id != t.id or a.client_id != c.id:
            raise HTTPException(404, "Array not found")
        if body.name is not None:
            new_name = body.name.strip()
            if new_name and new_name != a.name:
                clash = db.execute(
                    select(Array).where(
                        Array.tenant_id == t.id,
                        Array.name == new_name,
                        Array.id != a.id,
                    )
                ).scalar_one_or_none()
                if clash:
                    raise HTTPException(409,
                        "Another array already has that name")
                a.name = new_name
        for field in ("nepool_gis_id", "region",
                      "bill_offset_months", "notes", "excluded"):
            if field in body.model_fields_set:
                setattr(a, field, getattr(body, field))
        db.commit()
        accts = db.execute(
            select(UtilityAccount).where(UtilityAccount.array_id == a.id)
        ).scalars().all()
        return {"ok": True, "array": _array_to_dict(a, accts)}


@router.delete("/v1/account/clients/{client_id}/arrays/{array_id}")
def delete_array(client_id: int, array_id: int,
                 authorization: Optional[str] = Header(default=None)):
    """Soft-delete an array and its utility accounts.
    Returns an undo_token valid for 5 minutes."""
    t = tenant_from_session(authorization)
    now_ts = datetime.utcnow()
    with SessionLocal() as db:
        c = _resolve_client_for_tenant(db, t.id, client_id)
        a = db.get(Array, array_id)
        if not a or a.tenant_id != t.id or a.client_id != c.id:
            raise HTTPException(404, "Array not found")
        if a.deleted_at is not None:
            raise HTTPException(404, "Array not found")

        uas = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.array_id == a.id,
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all()
        ua_ids = [u.id for u in uas]
        for u in uas:
            u.deleted_at = now_ts
        a.deleted_at = now_ts

        undo_token = _make_undo_token(t.id, [array_id], now_ts)
        db.add(DeleteHistory(
            tenant_id=t.id,
            undo_token=undo_token,
            payload={"clients": [], "arrays": [array_id], "utility_accounts": ua_ids},
            expires_at=now_ts + timedelta(minutes=5),
        ))
        db.commit()
        new_count = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id, Array.deleted_at.is_(None)
            )
        ).scalar() or 0
        sub_id = t.stripe_subscription_id
        tenant_email = t.contact_email

    reconcile_subscription_quantity(sub_id, int(new_count), t.id, tenant_email)
    return {"ok": True, "undo_token": undo_token}


# ─── Bulk delete + undo ─────────────────────────────────────────────────


def _make_undo_token(tenant_id: str, ids: list[int], ts: datetime) -> str:
    raw = f"{tenant_id}:{sorted(ids)}:{ts.timestamp()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class BulkDeleteBody(BaseModel):
    ids: list[int]


class UndoBody(BaseModel):
    undo_token: str


@router.delete("/v1/account/arrays/bulk")
def bulk_delete_arrays(
    body: BulkDeleteBody,
    authorization: Optional[str] = Header(default=None),
):
    """Soft-delete multiple arrays (from any client under the tenant) in one shot.
    Returns an undo_token valid for 5 minutes."""
    t = tenant_from_session(authorization)
    if not body.ids:
        raise HTTPException(400, "ids must be non-empty")
    now_ts = datetime.utcnow()
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.id.in_(body.ids),
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
            )
        ).scalars().all()
        if not arrays:
            raise HTTPException(404, "No matching arrays found")
        array_ids = [a.id for a in arrays]

        uas = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.array_id.in_(array_ids),
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all()
        ua_ids = [u.id for u in uas]
        for u in uas:
            u.deleted_at = now_ts
        for a in arrays:
            a.deleted_at = now_ts

        undo_token = _make_undo_token(t.id, array_ids, now_ts)
        db.add(DeleteHistory(
            tenant_id=t.id,
            undo_token=undo_token,
            payload={"clients": [], "arrays": array_ids, "utility_accounts": ua_ids},
            expires_at=now_ts + timedelta(minutes=5),
        ))
        db.commit()
        new_count = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id, Array.deleted_at.is_(None)
            )
        ).scalar() or 0
        sub_id = t.stripe_subscription_id
        tenant_email = t.contact_email

    reconcile_subscription_quantity(sub_id, int(new_count), t.id, tenant_email)
    return {"ok": True, "soft_deleted": len(array_ids), "undo_token": undo_token}


@router.delete("/v1/account/clients-bulk")
def bulk_delete_clients(
    body: BulkDeleteBody,
    authorization: Optional[str] = Header(default=None),
):
    """Soft-delete multiple clients and cascade to their arrays + utility accounts.
    Returns an undo_token valid for 5 minutes."""
    t = tenant_from_session(authorization)
    if not body.ids:
        raise HTTPException(400, "ids must be non-empty")
    now_ts = datetime.utcnow()
    with SessionLocal() as db:
        clients = db.execute(
            select(Client).where(
                Client.id.in_(body.ids),
                Client.tenant_id == t.id,
                Client.deleted_at.is_(None),
            )
        ).scalars().all()
        if not clients:
            raise HTTPException(404, "No matching clients found")
        client_ids = [c.id for c in clients]

        arrays = db.execute(
            select(Array).where(
                Array.client_id.in_(client_ids),
                Array.deleted_at.is_(None),
            )
        ).scalars().all()
        array_ids = [a.id for a in arrays]

        ua_ids: list[int] = []
        if array_ids:
            uas = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.array_id.in_(array_ids),
                    UtilityAccount.deleted_at.is_(None),
                )
            ).scalars().all()
            ua_ids = [u.id for u in uas]
            for u in uas:
                u.deleted_at = now_ts
        for a in arrays:
            a.deleted_at = now_ts
        for c in clients:
            c.deleted_at = now_ts

        undo_token = _make_undo_token(t.id, client_ids, now_ts)
        db.add(DeleteHistory(
            tenant_id=t.id,
            undo_token=undo_token,
            payload={"clients": client_ids, "arrays": array_ids, "utility_accounts": ua_ids},
            expires_at=now_ts + timedelta(minutes=5),
        ))
        db.commit()
        new_count = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id, Array.deleted_at.is_(None)
            )
        ).scalar() or 0
        sub_id = t.stripe_subscription_id
        tenant_email = t.contact_email

    reconcile_subscription_quantity(sub_id, int(new_count), t.id, tenant_email)
    return {"ok": True, "soft_deleted": len(client_ids), "undo_token": undo_token}


@router.post("/v1/account/undo-delete")
def undo_delete(
    body: UndoBody,
    authorization: Optional[str] = Header(default=None),
):
    """Restore soft-deleted records referenced by undo_token.
    Only works within 5 minutes of the delete. Returns 410 if expired."""
    t = tenant_from_session(authorization)
    now_ts = datetime.utcnow()
    with SessionLocal() as db:
        history = db.execute(
            select(DeleteHistory).where(
                DeleteHistory.undo_token == body.undo_token,
                DeleteHistory.tenant_id == t.id,
            )
        ).scalar_one_or_none()
        if not history:
            raise HTTPException(404, "Undo token not found")
        if history.consumed_at is not None:
            raise HTTPException(409, "This undo token has already been used")
        if history.expires_at < now_ts:
            raise HTTPException(410, "Undo window expired — the 5-minute undo period has passed")

        payload = history.payload or {}
        restored_clients = payload.get("clients") or []
        restored_arrays = payload.get("arrays") or []
        restored_uas = payload.get("utility_accounts") or []

        if restored_clients:
            db.execute(
                Client.__table__.update()
                .where(Client.id.in_(restored_clients), Client.tenant_id == t.id)
                .values(deleted_at=None)
            )
        if restored_arrays:
            db.execute(
                Array.__table__.update()
                .where(Array.id.in_(restored_arrays), Array.tenant_id == t.id)
                .values(deleted_at=None)
            )
        if restored_uas:
            db.execute(
                UtilityAccount.__table__.update()
                .where(UtilityAccount.id.in_(restored_uas), UtilityAccount.tenant_id == t.id)
                .values(deleted_at=None)
            )

        history.consumed_at = now_ts
        db.commit()
        new_count = db.execute(
            select(func.count()).select_from(Array).where(
                Array.tenant_id == t.id, Array.deleted_at.is_(None)
            )
        ).scalar() or 0
        sub_id = t.stripe_subscription_id
        tenant_email = t.contact_email

    reconcile_subscription_quantity(sub_id, int(new_count), t.id, tenant_email)
    return {
        "ok": True,
        "restored_clients": len(restored_clients),
        "restored_arrays": len(restored_arrays),
    }


# ─── Utility account CRUD (per array) ───────────────────────────────────

class AccountCreate(BaseModel):
    provider: str
    account_number: str
    nickname: Optional[str] = None


class AccountUpdate(BaseModel):
    provider: Optional[str] = None
    account_number: Optional[str] = None
    nickname: Optional[str] = None


@router.post("/v1/account/clients/{client_id}/arrays/{array_id}/accounts")
def add_utility_account(client_id: int, array_id: int, body: AccountCreate,
                        authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    code = (body.provider or "").lower().strip()
    if code not in PROVIDER_CODES:
        raise HTTPException(400, f"Unknown provider '{body.provider}'")
    num = (body.account_number or "").strip()
    if not num:
        raise HTTPException(400, "account_number is required")
    with SessionLocal() as db:
        c = _resolve_client_for_tenant(db, t.id, client_id)
        a = db.get(Array, array_id)
        if not a or a.tenant_id != t.id or a.client_id != c.id:
            raise HTTPException(404, "Array not found")
        # idempotency: same provider + account_number per tenant already taken?
        existing = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == t.id,
                UtilityAccount.provider == code,
                UtilityAccount.account_number == num,
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409,
                "This utility account number is already linked to another array")
        ac = UtilityAccount(
            tenant_id=t.id, array_id=a.id, provider=code,
            account_number=num,
            nickname=(body.nickname or a.name).strip(),
        )
        db.add(ac); db.commit()
        return {"ok": True, "account": {
            "id": ac.id, "provider": ac.provider,
            "provider_label": (get_provider(ac.provider) or {}).get("label", ac.provider),
            "account_number": ac.account_number, "nickname": ac.nickname,
        }}


@router.delete("/v1/account/clients/{client_id}/arrays/{array_id}/accounts/{acct_id}")
def remove_utility_account(client_id: int, array_id: int, acct_id: int,
                           authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = _resolve_client_for_tenant(db, t.id, client_id)
        a = db.get(Array, array_id)
        if not a or a.tenant_id != t.id or a.client_id != c.id:
            raise HTTPException(404, "Array not found")
        ac = db.get(UtilityAccount, acct_id)
        if not ac or ac.tenant_id != t.id or ac.array_id != a.id:
            raise HTTPException(404, "Utility account not found")
        db.delete(ac); db.commit()
    return {"ok": True, "account_id": acct_id, "deleted": True}


# ─── Quarter helpers ─────────────────────────────────────────────────────

def _quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def _quarter_end_date(year: int, q: int) -> date:
    end_month = q * 3
    _, last = calendar.monthrange(year, end_month)
    return date(year, end_month, last)


def _parse_quarter_str(s: str) -> tuple[int, int]:
    """Parse 'Q1-2026' or 'Q1 2026' → (year, quarter_num). Raises ValueError."""
    m = re.match(r'^[Qq]([1-4])[-\s](\d{4})$', s.strip())
    if not m:
        raise ValueError(f"Invalid quarter format: {s!r}. Expected Q1-2026.")
    return int(m.group(2)), int(m.group(1))


def _quarter_to_reference_date(year: int, q: int) -> date:
    """Return the first date of the quarter AFTER (year, q).

    Passing this as reference_date to build_workbook makes (year, q) the
    last complete quarter in the rolling window."""
    end_month = q * 3
    if end_month == 12:
        return date(year + 1, 1, 1)
    return date(year, end_month + 1, 1)


# ─── Reports history ─────────────────────────────────────────────────────

@router.get("/v1/account/reports")
def get_reports(
    quarters: int = 6,
    authorization: Optional[str] = Header(default=None),
):
    """Return per-quarter snapshots for the last N quarters (default 6).

    Status derivation (no ReportRun table — derived from Bill + delivery):
      sent   — mwh_total > 0 and a client delivery was recorded after quarter end
      ready  — mwh_total > 0 but no qualifying delivery
      draft  — arrays exist but no bill data for this quarter (incl. in-progress)
      empty  — no arrays configured under this tenant
    """
    t = tenant_from_session(authorization)
    if quarters < 1:
        quarters = 1
    elif quarters > 12:
        quarters = 12

    today = date.today()
    cy, cq = today.year, _quarter_of(today.month)

    # Build quarter list: most-recent first, including the current in-progress one
    qlist: list[tuple[int, int]] = []
    y, q = cy, cq
    for _ in range(quarters):
        qlist.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
            )
        ).scalars().all()
        array_ids = [a.id for a in arrays]

        if array_ids:
            accounts = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.array_id.in_(array_ids),
                )
            ).scalars().all()
            account_ids = [a.id for a in accounts]
            account_to_array: dict[int, int] = {a.id: a.array_id for a in accounts}
        else:
            account_ids = []
            account_to_array = {}

        bills: list[Bill] = (
            db.execute(
                select(Bill).where(Bill.account_id.in_(account_ids))
            ).scalars().all()
            if account_ids else []
        )

        last_delivered: Optional[datetime] = db.execute(
            select(func.max(Client.last_delivery_at)).where(
                Client.tenant_id == t.id,
                Client.deleted_at.is_(None),
            )
        ).scalar()

    # Group bills: (year, quarter) → {array_id: kwh_total}
    bill_data: dict[tuple[int, int], dict[int, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for b in bills:
        if not b.kwh_generated or b.kwh_generated <= 0:
            continue
        src = b.period_start or b.bill_date
        if not src:
            continue
        key = (src.year, _quarter_of(src.month))
        arr_id = account_to_array.get(b.account_id)
        if arr_id is None:
            continue
        bill_data[key][arr_id] += b.kwh_generated

    has_arrays = bool(array_ids)
    result = []
    for (qy, qq) in qlist:
        is_current = (qy == cy and qq == cq)
        qdata = bill_data.get((qy, qq), {})
        array_count = len(qdata)
        mwh_total = round(sum(qdata.values()) / 1000.0, 3)

        if not has_arrays:
            status = "empty"
        elif is_current or mwh_total <= 0:
            status = "draft"
        elif last_delivered and last_delivered.date() >= _quarter_end_date(qy, qq):
            status = "sent"
        else:
            status = "ready"

        result.append({
            "quarter": f"Q{qq}-{qy}",
            "year": qy,
            "quarter_num": qq,
            "status": status,
            "array_count": array_count,
            "last_generated_at": None,
            "last_delivered_at": (
                last_delivered.isoformat() if last_delivered and status == "sent" else None
            ),
            "mwh_total": mwh_total,
        })

    return {"reports": result}


# ─── Next-run preview ────────────────────────────────────────────────────

@router.get("/v1/account/reports/next-run")
def get_reports_next_run(
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """STUB: Returns next scheduled delivery date and a current-quarter preview.

    Next-run date is derived from the account's report_frequency:
      - quarterly: 1st of the next quarter-start month (Jan/Apr/Jul/Oct)
      - monthly:   1st of next calendar month

    MWh/array preview is the current in-progress quarter's data so far
    (same Bill-aggregation logic as GET /v1/account/reports).

    TODO: When delivery history is stored per-run (ReportRun table), replace
    the bill-aggregation preview with real "arrays confirmed for next run" data.
    """
    t = tenant_from_session(authorization)
    today = date.today()
    freq = (t.report_frequency or "quarterly").lower()
    if freq not in ("monthly", "quarterly"):
        freq = "quarterly"

    # Compute next scheduled run date
    if freq == "monthly":
        if today.month == 12:
            next_run = date(today.year + 1, 1, 1)
        else:
            next_run = date(today.year, today.month + 1, 1)
    else:
        quarter_months = [1, 4, 7, 10]
        next_month = next(
            (m for m in quarter_months if m > today.month), None
        )
        if next_month is None:
            next_run = date(today.year + 1, 1, 1)
        else:
            next_run = date(today.year, next_month, 1)

    days_until = (next_run - today).days

    # Current-quarter preview — reuse the same Bill aggregation
    cy, cq = today.year, _quarter_of(today.month)

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
            )
        ).scalars().all()
        array_ids = [a.id for a in arrays]

        clients = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.deleted_at.is_(None),
                Client.active == True,  # noqa: E712
            )
        ).scalars().all()
        client_count = len(clients)

        if array_ids:
            ua_rows = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.array_id.in_(array_ids)
                )
            ).scalars().all()
            account_ids = [a.id for a in ua_rows]
            account_to_array: dict[int, int] = {
                a.id: a.array_id for a in ua_rows
            }
        else:
            account_ids = []
            account_to_array = {}

        bills: list[Bill] = (
            db.execute(
                select(Bill).where(Bill.account_id.in_(account_ids))
            ).scalars().all()
            if account_ids
            else []
        )

    kwh_total = 0.0
    arrays_with_data: set[int] = set()
    for b in bills:
        if not b.kwh_generated or b.kwh_generated <= 0:
            continue
        src = b.period_start or b.bill_date
        if not src:
            continue
        if (src.year, _quarter_of(src.month)) == (cy, cq):
            kwh_total += b.kwh_generated
            arr_id = account_to_array.get(b.account_id)
            if arr_id is not None:
                arrays_with_data.add(arr_id)

    mwh_preview = round(kwh_total / 1000.0, 3)
    preview_array_count = len(arrays_with_data) if arrays_with_data else len(array_ids)

    return {
        "next_run_date": next_run.isoformat(),
        "days_until": days_until,
        "frequency": freq,
        "array_count": preview_array_count,
        "mwh_preview": mwh_preview,
        "rec_preview": int(mwh_preview),
        "client_count": client_count,
    }


# ─── Regenerate ──────────────────────────────────────────────────────────

class RegenerateBody(BaseModel):
    quarter: Optional[str] = None
    client_id: Optional[int] = None


@router.post("/v1/account/regenerate")
def regenerate_report(
    body: RegenerateBody,
    authorization: Optional[str] = Header(default=None),
):
    """Rebuild workbook(s) for the given scope without sending email.

    If client_id provided: regenerate only that client.
    If omitted: regenerate all active clients under the tenant.
    If quarter provided (e.g. 'Q1-2026'): target that quarter's rolling window.
    Returns {status, generated_at}."""
    t = tenant_from_session(authorization)

    reference_date: Optional[date] = None
    if body.quarter:
        try:
            qy, qq = _parse_quarter_str(body.quarter)
            reference_date = _quarter_to_reference_date(qy, qq)
        except ValueError as e:
            raise HTTPException(400, str(e))

    generated_at = datetime.utcnow()

    from .writers import build_workbook

    if body.client_id is not None:
        with SessionLocal() as db:
            c = db.get(Client, body.client_id)
            if not c or c.tenant_id != t.id:
                raise HTTPException(404, "Client not found")
        tmpdir = tempfile.mkdtemp(prefix=f"so-regen-c{body.client_id}-")
        try:
            build_workbook(
                client_id=body.client_id,
                out_path=Path(tmpdir) / "report.xlsx",
                reference_date=reference_date,
            )
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(500, f"Regeneration failed: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        with SessionLocal() as db:
            client_rows = db.execute(
                select(Client).where(
                    Client.tenant_id == t.id,
                    Client.deleted_at.is_(None),
                    Client.active.is_(True),
                )
            ).scalars().all()
            client_ids = [c.id for c in client_rows]

        for cid in client_ids:
            tmpdir = tempfile.mkdtemp(prefix=f"so-regen-c{cid}-")
            try:
                build_workbook(
                    client_id=cid,
                    out_path=Path(tmpdir) / "report.xlsx",
                    reference_date=reference_date,
                )
            except Exception:
                pass
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

    return {"status": "regenerated", "generated_at": generated_at.isoformat() + "Z"}


# ─── Recent captures feed ────────────────────────────────────────────────

@router.get("/v1/account/recent-captures")
def recent_captures(
    limit: int = 5,
    authorization: Optional[str] = Header(default=None),
):
    """Return the last N bill captures for this tenant, annotated with client
    and array names. Powers the activity feed on the dashboard Account tab.

    Each entry: {pulled_at, client_name, array_name, period_start, period_end}.
    period_start/end are ISO strings or null if the bill parser didn't extract them."""
    if limit < 1:
        limit = 1
    elif limit > 50:
        limit = 50
    t = tenant_from_session(authorization)
    from .models import Bill
    with SessionLocal() as db:
        rows = db.execute(
            select(
                Bill.pulled_at,
                Client.name.label("client_name"),
                Array.name.label("array_name"),
                Bill.period_start,
                Bill.period_end,
            )
            .join(UtilityAccount, Bill.account_id == UtilityAccount.id)
            .join(Array, UtilityAccount.array_id == Array.id)
            .join(Client, Array.client_id == Client.id)
            .where(Bill.tenant_id == t.id)
            .order_by(Bill.pulled_at.desc())
            .limit(limit)
        ).all()
        return {
            "ok": True,
            "captures": [
                {
                    "pulled_at": r.pulled_at.isoformat() if r.pulled_at else None,
                    "client_name": r.client_name,
                    "array_name": r.array_name,
                    "period_start": r.period_start.isoformat() if r.period_start else None,
                    "period_end": r.period_end.isoformat() if r.period_end else None,
                }
                for r in rows
            ],
        }
