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
from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, Client, Array, LoginToken, now
from .notify import _send_via_resend, send_internal_alert

logger = logging.getLogger(__name__)

APP_URL = os.getenv("APP_URL", "https://solaroperator.org").rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")  # if blank, generated at startup
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
LOGIN_LINK_TTL_SECONDS = 15 * 60  # 15 minutes

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


# ─── Client (sub-client) CRUD ───────────────────────────────────────────

class ClientCreate(BaseModel):
    name: str
    contact_email: Optional[EmailStr] = None
    cc_emails: Optional[str] = None  # comma-separated
    report_frequency: Optional[str] = None  # null = inherit tenant cadence
    notes: Optional[str] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    cc_emails: Optional[str] = None
    report_frequency: Optional[str] = None
    active: Optional[bool] = None
    notes: Optional[str] = None


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
    }


# ─── magic-link auth ────────────────────────────────────────────────────

@router.post("/v1/auth/request")
def auth_request(req: AuthRequest):
    """Email a one-time login link to a known customer. Always returns OK
    (don't leak which emails are registered)."""
    email = req.email.lower().strip()
    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.contact_email == email)
        ).scalars().first()
        if not t:
            # Don't leak — pretend it worked
            return {"ok": True, "delivered": True}

        token = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(seconds=LOGIN_LINK_TTL_SECONDS)
        db.add(LoginToken(token=token, tenant_id=t.id, email=email, expires_at=expires))
        db.commit()
        tenant_name = t.name

    link = f"{APP_URL}/account.html?token={token}"
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

    session_token = _sign_session(tenant_id)
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
        return {
            "tenant_id": t.id,
            "name": t.name,
            "email": t.contact_email,
            "plan": t.plan,
            "active": t.active,
            "subscription_status": t.subscription_status,
            "report_frequency": t.report_frequency,
            "last_pull_at": t.last_pull_at.isoformat() if t.last_pull_at else None,
            "last_delivery_at": t.last_delivery_at.isoformat() if t.last_delivery_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "accounts_count": len(accounts),
            "bills_count": len(bills_count),
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


@router.post("/v1/account/send-report")
def send_my_report(authorization: Optional[str] = Header(default=None)):
    """Customer-triggered: 'send me my latest report now.'"""
    t = tenant_from_session(authorization)
    if not t.active and t.subscription_status not in ("active", "trialing", "comped"):
        raise HTTPException(402, "Reactivate your subscription to send reports")
    # Defer heavy import (avoid circulars at module load)
    from .delivery import deliver_for_tenant
    return deliver_for_tenant(t.id, override_to=None, triggered_by="self-serve")


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
            return_url=f"{APP_URL}/account.html",
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
        out = []
        for c in clients:
            n_arr = db.execute(
                select(Array).where(Array.client_id == c.id)
            ).scalars().all()
            out.append(_client_to_dict(c, array_count=len(n_arr)))
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
        for field in ("contact_email", "cc_emails", "report_frequency",
                      "active", "notes"):
            v = getattr(body, field)
            if v is not None:
                setattr(c, field, v)
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
