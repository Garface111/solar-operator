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

import os
import secrets
import logging
import hmac
import hashlib
import base64
import json
import time
from datetime import datetime, timedelta
from typing import Optional

import stripe
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, func

from .db import SessionLocal
from .models import Tenant, Client, Array, LoginToken, UtilityAccount, now
from .notify import _send_via_resend, send_internal_alert, FROM_ADDRESS
from .providers import PROVIDERS, PROVIDER_CODES, get_provider
from .email_templates import (
    DEFAULT_SUBJECT_TEMPLATE, DEFAULT_BODY_TEMPLATE, DEFAULT_DASHBOARD_URL,
    MERGE_TAGS, render_email, resolve_from_header,
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
# same $45 default used by the onboarding checkout (ONBOARDING_ARRAY_CENTS).
STRIPE_ARRAY_PRICE_ID = os.getenv("STRIPE_ARRAY_PRICE_ID", "")
ARRAY_PRICE_CENTS = int(os.getenv("ONBOARDING_ARRAY_CENTS", "4500"))  # $45/array/mo
_array_price_cache: dict = {}


def _array_price_cents() -> tuple[int, str]:
    """Monthly per-array price in (cents, currency).

    Prefers the live Stripe price referenced by STRIPE_ARRAY_PRICE_ID and caches
    the result for the process lifetime; falls back to the hardcoded $45 default
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
    report_frequency: Optional[str] = None  # null = inherit tenant cadence
    notes: Optional[str] = None
    # GMP auto-populate (mirrors onboarding Screen 4 — editable post-onboarding).
    # The operator logs into GMP with either an email or a username; we match on
    # whichever is set when the extension captures a session.
    gmp_email: Optional[EmailStr] = None
    gmp_username: Optional[str] = None
    gmp_autopopulate: Optional[bool] = None


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
        "last_delivered_at": c.last_delivered_at.isoformat() if c.last_delivered_at else None,
        "last_bounced_at": c.last_bounced_at.isoformat() if c.last_bounced_at else None,
        "last_bounce_reason": c.last_bounce_reason,
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


def _array_to_dict(a: Array, accounts: list[UtilityAccount]) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "nepool_gis_id": a.nepool_gis_id,
        "region": a.region,
        "bill_offset_months": a.bill_offset_months,
        "notes": a.notes,
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

def issue_magic_link(email: str) -> bool:
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
        db.add(LoginToken(token=token, tenant_id=t.id, email=email, expires_at=expires))
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
    _send_via_resend(
        to=email,
        subject="Sign in to Solar Operator",
        html=html,
        text=text,
    )
    return True


@router.post("/v1/auth/request")
def auth_request(req: AuthRequest):
    """Email a one-time login link to a known customer. Always returns OK
    (don't leak which emails are registered)."""
    issue_magic_link(req.email)
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
        db.commit()

    session_token = mint_session_for_tenant(tenant_id)
    return {"ok": True, "session_token": session_token, "expires_in": SESSION_TTL_SECONDS}


# ─── account read ───────────────────────────────────────────────────────

@router.get("/v1/account")
def account_me(authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        from .models import UtilityAccount, UtilitySession, Bill
        # Re-read inside this session for fresh relationships
        t = db.get(Tenant, t.id)
        accounts = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == t.id)
        ).scalars().all()
        last_sess = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == t.id)
            .order_by(UtilitySession.captured_at.desc())
        ).scalars().first()
        bills_count = db.execute(
            select(Bill).where(Bill.tenant_id == t.id)
        ).scalars().all()
        clients_count = db.execute(
            select(func.count()).select_from(Client).where(Client.tenant_id == t.id)
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
            "default_email_subject": DEFAULT_SUBJECT_TEMPLATE,
            "default_email_body": DEFAULT_BODY_TEMPLATE,
            "merge_tags": list(MERGE_TAGS),
            "last_pull_at": t.last_pull_at.isoformat() if t.last_pull_at else None,
            "last_delivery_at": t.last_delivery_at.isoformat() if t.last_delivery_at else None,
            "extension_heartbeat_at": t.extension_heartbeat_at.isoformat() if t.extension_heartbeat_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "accounts_count": len(accounts),
            "bills_count": len(bills_count),
            "clients_count": int(clients_count),
            "session": {
                "captured_at": last_sess.captured_at.isoformat() if last_sess else None,
                "expires_at": last_sess.expires_at.isoformat() if last_sess and last_sess.expires_at else None,
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
    if body.frequency not in ("weekly", "monthly", "quarterly"):
        raise HTTPException(400, "frequency must be weekly, monthly, or quarterly")
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

# Fake values for the live preview (POST /v1/account/email-preview). A fixed
# quarter keeps the preview deterministic regardless of when it's rendered.
_PREVIEW_CLIENT = "Sample Client"
_PREVIEW_CTX = {
    "client_name": _PREVIEW_CLIENT,
    "quarter": "2026 Q2",
    "arrays_count": 3,
    "period_start": "Apr 1, 2026",
    "period_end": "Jun 30, 2026",
    "dashboard_url": DEFAULT_DASHBOARD_URL,
}
_VALID_SEND_MODES = ("to_client", "to_me")


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
            raise HTTPException(400, "send_mode must be 'to_client' or 'to_me'")


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

    ctx = dict(_PREVIEW_CTX, tenant_name=tenant_name, tenant_email=tenant_email)
    subject, html, text = render_email(
        subject_template=eff_subject, body_template=eff_body, ctx=ctx)
    from_header = resolve_from_header(eff_from_email, eff_from_name, tenant_name)
    if eff_mode == "to_me":
        recipient = tenant_email or "you (your account email)"
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


@router.post("/v1/account/send-report")
def send_my_report(authorization: Optional[str] = Header(default=None)):
    """Customer-triggered: 'send me my latest report now.'"""
    t = tenant_from_session(authorization)
    if not t.active and t.subscription_status not in ("active", "trialing", "comped"):
        raise HTTPException(402, "Reactivate your subscription to send reports")
    # Defer heavy import (avoid circulars at module load)
    from .delivery import deliver_for_tenant
    return deliver_for_tenant(t.id, override_to=None, triggered_by="self-serve")


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
    return {**result, "sample": True, "sent_to": tenant_email}


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
            select(func.count()).select_from(Array).where(Array.tenant_id == t.id)
        ).scalar() or 0
    cents, currency = _array_price_cents()
    billable = int(billable)
    return {
        "billable_arrays": billable,
        "price_cents": cents,
        "total_cents": billable * cents,
        "currency": currency,
    }


@router.get("/v1/account/billing-portal")
def billing_portal(authorization: Optional[str] = Header(default=None)):
    """Return a Stripe Billing Portal URL the customer can use to update card,
    download invoices, or cancel."""
    t = tenant_from_session(authorization)
    if not t.stripe_customer_id:
        raise HTTPException(404, "No Stripe customer linked — contact support@solaroperator.org")
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
            select(Client).where(Client.tenant_id == t.id)
                          .order_by(Client.name.asc())
        ).scalars().all()
        # Fetch all array counts in a single query rather than one per client.
        array_counts_rows = db.execute(
            select(Array.client_id, func.count(Array.id).label("n"))
            .where(Array.client_id.in_([c.id for c in clients]))
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
            "weekly", "monthly", "quarterly"):
        raise HTTPException(400,
            "report_frequency must be weekly, monthly, quarterly, or null")
    with SessionLocal() as db:
        existing = db.execute(
            select(Client).where(Client.tenant_id == t.id, Client.name == name)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "A client with that name already exists")
        c = Client(
            tenant_id=t.id, name=name,
            contact_email=body.contact_email,
            cc_emails=body.cc_emails,
            report_frequency=body.report_frequency,
            notes=body.notes,
            gmp_email=(body.gmp_email.lower().strip() if body.gmp_email else None),
            gmp_username=(body.gmp_username.strip()
                          if body.gmp_username and body.gmp_username.strip() else None),
            gmp_autopopulate=bool(body.gmp_autopopulate),
            active=True,
        )
        db.add(c); db.commit(); db.refresh(c)
        return {"ok": True, "client": _client_to_dict(c, 0)}


@router.patch("/v1/account/clients/{client_id}")
def update_client(client_id: int, body: ClientUpdate,
                  authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    if body.report_frequency and body.report_frequency not in (
            "weekly", "monthly", "quarterly"):
        raise HTTPException(400,
            "report_frequency must be weekly, monthly, quarterly, or null")
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
        # Use model_fields_set so an explicit null in the payload can clear a
        # field (e.g. report_frequency=null → inherit from account). Fields
        # absent from the request body are NOT in model_fields_set and are
        # left unchanged — the standard PATCH semantic.
        for field in ("contact_email", "cc_emails", "report_frequency",
                      "active", "notes", "gmp_autopopulate"):
            if field in body.model_fields_set:
                setattr(c, field, getattr(body, field))
        if body.gmp_email is not None:
            c.gmp_email = body.gmp_email.lower().strip() or None
        if body.gmp_username is not None:
            c.gmp_username = body.gmp_username.strip() or None
        db.commit(); db.refresh(c)
        n_arr = db.execute(
            select(Array).where(Array.client_id == c.id)
        ).scalars().all()
        return {"ok": True, "client": _client_to_dict(c, len(n_arr))}


@router.delete("/v1/account/clients/{client_id}")
def delete_client(client_id: int,
                  authorization: Optional[str] = Header(default=None)):
    """Soft delete: marks the client inactive. Arrays stay linked so we
    don't orphan bills; reactivate by PATCHing active=True."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
        c.active = False
        db.commit()
    return {"ok": True, "client_id": client_id, "active": False}


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
                           authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = db.get(Client, client_id)
        if not c or c.tenant_id != t.id:
            raise HTTPException(404, "Client not found")
    if not t.active and t.subscription_status not in (
            "active", "trialing", "comped"):
        raise HTTPException(402, "Reactivate your subscription to send reports")
    from .delivery import deliver_for_client
    return deliver_for_client(client_id, triggered_by="self-serve")


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
            select(Array).where(Array.client_id == c.id)
                         .order_by(Array.name.asc())
        ).scalars().all()
        array_ids = [a.id for a in arrays]
        # Fetch all utility accounts in one query rather than one per array.
        all_accts = db.execute(
            select(UtilityAccount)
            .where(UtilityAccount.array_id.in_(array_ids))
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
        return {"ok": True, "array": _array_to_dict(arr, accts)}


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
                      "bill_offset_months", "notes"):
            v = getattr(body, field)
            if v is not None:
                setattr(a, field, v)
        db.commit()
        accts = db.execute(
            select(UtilityAccount).where(UtilityAccount.array_id == a.id)
        ).scalars().all()
        return {"ok": True, "array": _array_to_dict(a, accts)}


@router.delete("/v1/account/clients/{client_id}/arrays/{array_id}")
def delete_array(client_id: int, array_id: int,
                 authorization: Optional[str] = Header(default=None)):
    """Hard delete an array. Its UtilityAccount rows are cascaded; Bills
    belonging to those accounts are also removed via FK cascade.

    Use with care — there is no undo. Soft-disable is at the Client layer."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        c = _resolve_client_for_tenant(db, t.id, client_id)
        a = db.get(Array, array_id)
        if not a or a.tenant_id != t.id or a.client_id != c.id:
            raise HTTPException(404, "Array not found")
        db.delete(a); db.commit()
    return {"ok": True, "array_id": array_id, "deleted": True}


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
