"""V2 offtaker pay-links — Stripe Connect destination charges + platform fee.

Owners (Array Operator tenants) connect an Express account once. Each invoice
send can mint a Checkout Session whose PaymentIntent:

  * charges the offtaker the invoice amount,
  * keeps application_fee_amount for the platform (EnergyAgent),
  * transfers the rest to the owner's connected account.

See docs/plans/2026-07-13-offtaker-pay-links-v2.md.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from datetime import datetime
from typing import Any, Optional

import stripe
from sqlalchemy import select

logger = logging.getLogger(__name__)

# Stripe hard-caps a Checkout Session's life at 24h; mint just under it.
CHECKOUT_TTL_SECONDS = 23 * 3600
# A Session with less than this left is re-minted on click rather than handed
# to the offtaker, so nobody starts a checkout that dies under them.
CHECKOUT_REFRESH_GRACE_SECONDS = 15 * 60
# Path of the durable per-invoice pay link, served through the product domain's
# same-origin /v1 proxy (see array-operator public/_redirects).
PAY_PATH = "/v1/array-operator/billing/pay/"

# CHARGE MODEL (Sep 2026, Ford). Who pays Stripe's processing fee:
#   "direct"      — the Checkout Session and charge live ON the operator's
#                   connected account. The OPERATOR pays Stripe's fee (card
#                   2.9% + 30¢, ACH debit 0.8% capped at $5), the platform
#                   receives only its application fee, the offtaker's card
#                   statement carries the OPERATOR's name, and the money never
#                   crosses the platform balance ("we never hold your money").
#   "destination" — legacy: charge on the platform, funds transferred to the
#                   operator. The PLATFORM eats Stripe's fee against a 0.5%
#                   application fee — a loss on every card payment.
# Resolution: Tenant.offtaker_charge_model → AO_OFFTAKER_CHARGE_MODEL → direct.
DEFAULT_CHARGE_MODEL = "direct"
CHARGE_MODELS = ("direct", "destination")


def charge_model_for(tenant) -> str:
    v = (getattr(tenant, "offtaker_charge_model", None) or "").strip().lower()
    if v not in CHARGE_MODELS:
        v = (os.getenv("AO_OFFTAKER_CHARGE_MODEL", DEFAULT_CHARGE_MODEL) or "").strip().lower()
    return v if v in CHARGE_MODELS else DEFAULT_CHARGE_MODEL


def _stripe_kw(row) -> dict:
    """Request kwargs addressing the account a row's Session lives on: the
    operator's connected account for a direct charge, nothing (the platform)
    for a legacy destination-charge row. Retrieve/expire/refund of a direct
    charge's objects FAIL without it — they do not exist on the platform."""
    acct = getattr(row, "stripe_account_id", None)
    return {"stripe_account": acct} if acct else {}


def payment_method_types() -> Optional[list[str]]:
    """Optional pin of Checkout's payment methods, e.g.
    AO_OFFTAKER_PAYMENT_METHODS="us_bank_account,card". Unset → Stripe's
    automatic methods for the CHARGING account (for direct charges: what is
    enabled for connected accounts under Dashboard → Settings → Connect →
    Payment methods, which is where ACH gets switched on)."""
    raw = (os.getenv("AO_OFFTAKER_PAYMENT_METHODS", "") or "").strip()
    if not raw:
        return None
    items = [p.strip() for p in raw.split(",") if p.strip()]
    return items or None

# Platform fee: basis points of the invoice total (50 = 0.5%). "Scrape a tiny
# bit" — env-driven so Ford can retune without a code push. Min floor optional.
DEFAULT_FEE_BPS = 50
DEFAULT_FEE_MIN_CENTS = 0


def fee_bps() -> int:
    try:
        return max(0, int(os.getenv("AO_OFFTAKER_FEE_BPS", str(DEFAULT_FEE_BPS))))
    except (TypeError, ValueError):
        return DEFAULT_FEE_BPS


def fee_min_cents() -> int:
    try:
        return max(0, int(os.getenv("AO_OFFTAKER_FEE_MIN_CENTS", str(DEFAULT_FEE_MIN_CENTS))))
    except (TypeError, ValueError):
        return DEFAULT_FEE_MIN_CENTS


def payments_enabled() -> bool:
    """Hard kill-switch. Default ON — pay links still only fire when Connect is
    ready and amount > 0, so greenfield tenants are unaffected."""
    return (os.getenv("AO_OFFTAKER_PAYMENTS", "1") or "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def application_fee_cents(amount_cents: int,
                          bps: Optional[int] = None,
                          min_cents: Optional[int] = None) -> int:
    """Platform cut in cents for an invoice of `amount_cents`.

    Pure integer math (no float drift). Fee never exceeds the amount (Stripe
    rejects application_fee_amount >= charge amount).
    """
    if amount_cents <= 0:
        return 0
    b = fee_bps() if bps is None else max(0, int(bps))
    m = fee_min_cents() if min_cents is None else max(0, int(min_cents))
    fee = (int(amount_cents) * b) // 10_000
    fee = max(fee, m)
    # Leave the connected account at least 1¢ when amount > 1, else 0.
    if amount_cents <= 1:
        return 0
    return min(fee, amount_cents - 1)


def dollars_to_cents(amount: Any) -> int:
    """Round half-up to whole cents. Rejects negative / non-numeric → 0."""
    try:
        x = float(amount)
    except (TypeError, ValueError):
        return 0
    if x <= 0:
        return 0
    return int(round(x * 100))


def _stripe_ready() -> bool:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        return False
    stripe.api_key = key
    return True


def connect_ready(tenant) -> bool:
    """True when this owner can receive destination charges."""
    acct = getattr(tenant, "stripe_connect_account_id", None)
    return bool(acct and getattr(tenant, "stripe_connect_charges_enabled", False))


def link_existing_connect_account(db, tenant) -> dict:
    """If this tenant has no stripe_connect_account_id yet, find a matching
    Express account on our platform by metadata.tenant_id
    and attach it. Fixes the 'I finished Stripe KYC but pay links never mint'
    case when the owner set up Connect under a different session/tenant row
    or the DB write didn't land on the tenant they're sending from.
    """
    if getattr(tenant, "stripe_connect_account_id", None):
        return {"ok": True, "linked": False, "account_id": tenant.stripe_connect_account_id}
    if not _stripe_ready():
        return {"ok": False, "error": "Stripe not configured"}
    tid = str(getattr(tenant, "id", "") or "")
    try:
        # Page through platform connected accounts (small platforms = fine).
        starting_after = None
        matched = None
        for _ in range(10):  # up to 1000 accounts
            kwargs = {"limit": 100}
            if starting_after:
                kwargs["starting_after"] = starting_after
            page = stripe.Account.list(**kwargs)
            data = page.get("data") if isinstance(page, dict) else list(page.data or [])
            if not data:
                break
            for a in data:
                ad = a if isinstance(a, dict) else a.to_dict() if hasattr(a, "to_dict") else dict(a)
                meta = ad.get("metadata") or {}
                if tid and meta.get("tenant_id") == tid:
                    matched = ad
                    break
            if matched:
                break
            starting_after = data[-1].get("id") if isinstance(data[-1], dict) else getattr(data[-1], "id", None)
            has_more = page.get("has_more") if isinstance(page, dict) else getattr(page, "has_more", False)
            if not has_more:
                break
        if not matched:
            return {"ok": True, "linked": False, "account_id": None}
        acct_id = matched.get("id")
        # Email is not tenant identity. Also reject an account already attached
        # elsewhere even when its Stripe metadata was changed independently.
        from ..models import Tenant
        owner = db.execute(select(Tenant.id).where(
            Tenant.stripe_connect_account_id == acct_id,
            Tenant.id != tenant.id)).scalars().first()
        if not acct_id or owner is not None:
            return {"ok": False, "error": "Connect account ownership conflict"}
        enabled = bool(matched.get("charges_enabled"))
        tenant.stripe_connect_account_id = acct_id
        tenant.stripe_connect_charges_enabled = enabled
        db.commit()
        logger.info("linked existing Connect account %s → tenant %s (charges=%s)",
                    acct_id, tid, enabled)
        return {
            "ok": True, "linked": True, "account_id": acct_id,
            "charges_enabled": enabled,
            "details_submitted": bool(matched.get("details_submitted")),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("link_existing_connect_account failed for %s: %s", tid, e)
        return {"ok": False, "error": str(e)[:200]}


# ─── Connect Express onboarding ─────────────────────────────────────────────

def create_or_get_connect_account(db, tenant) -> dict:
    """Ensure the tenant has an Express Connect account; return {account_id, …}.

    Does NOT create an Account Link — callers that need onboarding UI call
    create_account_link() next. Idempotent: reuses existing account id.
    """
    if not _stripe_ready():
        return {"ok": False, "error": "Stripe not configured"}

    existing = getattr(tenant, "stripe_connect_account_id", None)
    if existing:
        # Refresh charges_enabled from Stripe so a completed KYC flips the flag
        # without waiting for account.updated.
        try:
            acct = stripe.Account.retrieve(existing)
            enabled = bool(getattr(acct, "charges_enabled", None)
                           or (acct.get("charges_enabled") if isinstance(acct, dict) else False))
            details = bool(getattr(acct, "details_submitted", None)
                           or (acct.get("details_submitted") if isinstance(acct, dict) else False))
            if enabled != bool(tenant.stripe_connect_charges_enabled):
                tenant.stripe_connect_charges_enabled = enabled
                db.commit()
            return {
                "ok": True,
                "account_id": existing,
                "charges_enabled": enabled,
                "details_submitted": details,
                "created": False,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("connect retrieve failed for %s: %s", tenant.id, e)
            return {"ok": True, "account_id": existing,
                    "charges_enabled": bool(tenant.stripe_connect_charges_enabled),
                    "created": False, "warning": str(e)[:200]}

    try:
        create_kwargs = dict(
            type="express",
            country="US",
            email=(tenant.contact_email or None),
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
                # ACH debit on the operator's own account (direct charges):
                # 0.8% capped at $5 instead of 2.9% + 30¢ for a card.
                "us_bank_account_ach_payments": {"requested": True},
            },
            business_type="individual",  # solar array owners; can be upgraded
            metadata={
                "tenant_id": str(tenant.id),
                "product": "array_operator",
                "kind": "offtaker_payouts",
            },
        )
        # Pre-fill email so Stripe's form asks for less typing.
        if tenant.contact_email:
            create_kwargs["individual"] = {"email": tenant.contact_email}
        acct = stripe.Account.create(**create_kwargs)
        acct_id = acct["id"] if isinstance(acct, dict) else acct.id
        tenant.stripe_connect_account_id = acct_id
        tenant.stripe_connect_charges_enabled = False
        db.commit()
        return {
            "ok": True,
            "account_id": acct_id,
            "charges_enabled": False,
            "details_submitted": False,
            "created": True,
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("Connect Account.create failed for %s", tenant.id)
        return {"ok": False, **_friendly_connect_error(e)}


def _friendly_connect_error(exc: Exception) -> dict:
    """Map raw Stripe errors to owner-safe codes + copy. Never leak request ids."""
    msg = str(exc) or ""
    low = msg.lower()
    # Platform hasn't finished https://dashboard.stripe.com/connect once.
    if "signed up for connect" in low or "dashboard.stripe.com/connect" in low:
        try:
            from ..notify import send_internal_alert
            send_internal_alert(
                "⚠️ Stripe Connect not activated (blocks offtaker pay-links)",
                "An owner tried Enable online pay but Stripe rejected Account.create:\n"
                "the Energy Agent Stripe account has not signed up for Connect yet.\n\n"
                "ONE-TIME FIX (Ford): open https://dashboard.stripe.com/connect "
                "while logged into the Energy Agent account, complete Get started, "
                "then owners can one-click bank setup again.\n\n"
                f"Raw: {msg[:300]}"
            )
        except Exception:  # noqa: BLE001
            pass
        return {
            "error": (
                "Online payments is finishing a one-time platform setup. "
                "Please try again in a few minutes, or email us and we'll turn it on."
            ),
            "error_code": "platform_connect_not_ready",
            "retryable": True,
        }
    if "rate" in low and "limit" in low:
        return {
            "error": "Stripe is busy — wait a few seconds and try again.",
            "error_code": "rate_limited",
            "retryable": True,
        }
    return {
        "error": "Couldn't start bank setup right now. Please try again shortly.",
        "error_code": "connect_create_failed",
        "retryable": True,
    }


def create_account_link(tenant, *, refresh_url: str, return_url: str) -> dict:
    """Mint a one-time Stripe Account Link for Express onboarding / updates."""
    if not _stripe_ready():
        return {"ok": False, "error": "Stripe not configured"}
    acct_id = getattr(tenant, "stripe_connect_account_id", None)
    if not acct_id:
        return {"ok": False, "error": "no connect account — call create first"}
    try:
        link = stripe.AccountLink.create(
            account=acct_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
        url = link["url"] if isinstance(link, dict) else link.url
        return {"ok": True, "url": url, "account_id": acct_id}
    except Exception as e:  # noqa: BLE001
        logger.exception("AccountLink.create failed for %s", tenant.id)
        return {"ok": False, "error": str(e)[:300]}


def refresh_connect_status(db, tenant) -> dict:
    """Pull charges_enabled / details_submitted from Stripe onto the tenant."""
    if not getattr(tenant, "stripe_connect_account_id", None):
        return {"ok": True, "connected": False, "charges_enabled": False,
                "details_submitted": False}
    return create_or_get_connect_account(db, tenant) | {"connected": True}


# ─── Per-invoice Checkout Session ───────────────────────────────────────────

def _period_key(match) -> str:
    ci = (match.computed_invoice if match else None) or {}
    # Prefer period_end (stable, used by exactly-once guard) then full range.
    pe = ci.get("period_end") or ""
    ps = ci.get("period_start") or ""
    if pe:
        return str(pe)[:40]
    if ps and pe:
        return f"{ps} → {pe}"[:40]
    inv = ci.get("invoice_number") or ""
    return str(inv)[:40] or datetime.utcnow().strftime("%Y-%m-%d")


def _amount_cents_from_match(match) -> int:
    ci = (match.computed_invoice if match else None) or {}
    # Budget override: the actual bill is the budgeted amount.
    if ci.get("budget_override") and ci.get("amount_owed") is not None:
        return dollars_to_cents(ci.get("amount_owed"))
    return dollars_to_cents(ci.get("amount_owed"))


def durable_pay_url(tenant, token: str) -> str:
    """The url that goes on the invoice and in the email: OURS, not Stripe's.

    A Checkout Session dies 24h after it is minted (Stripe hard cap) while the
    invoice says "due within 28 days" — a town clerk opening the email on day
    three used to land on Stripe's "session expired" page, and a re-send handed
    out the same dead url. This url is stable for the life of the invoice; the
    click (resolve_pay_link) mints or refreshes the Session on demand."""
    from ..branding import app_url
    base = app_url(getattr(tenant, "product", "array_operator")).rstrip("/")
    return f"{base}{PAY_PATH}{token}"


def _mint_checkout_session(*, tenant, row, customer_email: Optional[str],
                           period_label: str = "") -> dict:
    """Create the Checkout Session for an OfftakerPayment row (destination
    charge + platform application fee). Returns {id, url, payment_intent,
    expires_at (epoch seconds)}. Raises on Stripe errors — callers decide how
    to surface them. Shared by the first mint at send time and every re-mint
    from the durable link, so the two can never charge different shapes."""
    from ..branding import app_url
    base = app_url(getattr(tenant, "product", "array_operator")).rstrip("/")
    # Public offtaker thank-you page — NEVER the owner dashboard.
    # (Ford 2026-07-13: success used to land on /?paid=1#reports, so a pay-
    # link open in the owner's browser dropped the offtaker into Array Operator
    # with whatever so_session was already there. Offtakers must not enter the
    # app; paid.html has no auth, no SPA, and tells them to close the tab.)
    success_url = f"{base}/paid?status=ok"
    cancel_url = f"{base}/paid?status=cancel"
    inv_no = str(row.invoice_number or row.period_key)
    cust = row.customer_name or "Offtaker"
    operator = (getattr(tenant, "company_name", None) or getattr(tenant, "name", None)
                or "your solar provider")
    meta = {
        "kind": "offtaker_invoice",
        "tenant_id": str(tenant.id),
        "subscription_id": str(row.subscription_id),
        "payment_id": str(row.id),
        "invoice_number": inv_no[:40],
        "period_key": str(row.period_key)[:40],
    }
    # Stripe Checkout requires expires_at < 24h from creation (prod log
    # 2026-07-13: 30-day expires_at → pay links never minted). Use time.time()
    # NOT datetime.utcnow().timestamp() — the latter treats naive UTC as local
    # time and can land >24h out on non-UTC hosts.
    expires_at = int(time.time()) + CHECKOUT_TTL_SECONDS
    model = charge_model_for(tenant)
    acct = tenant.stripe_connect_account_id
    pi_data: dict = {
        "application_fee_amount": int(row.fee_cents or 0),
        "metadata": meta,
        "description": f"Solar credit · {cust} · {inv_no}"[:500],
    }
    extra: dict = {}
    if model == "direct":
        # The Session is created ON the operator's account (Stripe-Account
        # header). Stripe debits its processing fee from THEIR balance and
        # transfers application_fee_amount to the platform.
        extra["stripe_account"] = acct
    else:
        pi_data["transfer_data"] = {"destination": acct}
    pmt = payment_method_types()
    if pmt:
        extra["payment_method_types"] = pmt
    create_kwargs = dict(
        mode="payment",
        success_url=success_url + "&session_id={CHECKOUT_SESSION_ID}",
        cancel_url=cancel_url,
        customer_email=(customer_email or None),
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": row.currency or "usd",
                "unit_amount": int(row.amount_cents),
                "product_data": {
                    "name": f"Solar credit invoice {inv_no}",
                    "description": (
                        f"{cust} · {period_label or row.period_key} · payable to {operator}"
                    )[:500],
                },
            },
        }],
        payment_intent_data=pi_data,
        metadata=meta,
        expires_at=expires_at,
        **extra,
    )
    try:
        session = stripe.checkout.Session.create(**create_kwargs)
    except stripe.error.InvalidRequestError as e:
        # A pinned method the charging account cannot take yet (e.g. ACH before
        # its capability is active) must not cost the offtaker the pay button:
        # fall back to Stripe's automatic methods for that account.
        _msg = str(e).lower()
        if create_kwargs.get("payment_method_types") and (
                getattr(e, "param", None) == "payment_method_types"
                or "payment_method" in _msg or "payment method" in _msg):
            logger.warning("Checkout rejected pinned payment methods %s on %s (%s) — "
                           "retrying with automatic methods", pmt, acct, e)
            create_kwargs.pop("payment_method_types", None)
            session = stripe.checkout.Session.create(**create_kwargs)
        else:
            raise
    sess_id = session["id"] if isinstance(session, dict) else session.id
    url = session["url"] if isinstance(session, dict) else session.url
    pi = session.get("payment_intent") if isinstance(session, dict) else getattr(session, "payment_intent", None)
    if isinstance(pi, dict):
        pi = pi.get("id")
    return {"id": sess_id, "url": url,
            "payment_intent": pi if isinstance(pi, str) else None,
            "expires_at": expires_at,
            "charge_model": model,
            "account": acct if model == "direct" else None}


def create_offtaker_payment(db, *, tenant, sub, match,
                            force: bool = False) -> dict:
    """Create (or reuse) an OfftakerPayment + Checkout Session for this invoice.

    Returns a dict always:
      {ok, pay_url?, payment_id?, fee_cents?, amount_cents?, skipped?, error?}

    Never raises into the delivery path — Stripe / DB failures become ok=False
    so the classic invoice email still goes out.
    """
    from ..models import OfftakerPayment

    if not payments_enabled():
        return {"ok": False, "skipped": True, "error": "pay-links disabled"}
    if not _stripe_ready():
        return {"ok": False, "skipped": True, "error": "Stripe not configured"}
    # Auto-attach a Connect account finished under another tenant row / session
    # (common when the operator set up payouts once, then sends from the real
    # fleet tenant). Best-effort — never blocks the skip path below.
    if not getattr(tenant, "stripe_connect_account_id", None):
        try:
            link_existing_connect_account(db, tenant)
            db.refresh(tenant)
        except Exception:  # noqa: BLE001
            logger.warning("auto-link Connect failed for %s", getattr(tenant, "id", "?"),
                           exc_info=True)
    if not connect_ready(tenant):
        # One more refresh in case charges_enabled flipped after KYC.
        try:
            create_or_get_connect_account(db, tenant)
            db.refresh(tenant)
        except Exception:  # noqa: BLE001
            pass
    if not connect_ready(tenant):
        return {"ok": False, "skipped": True,
                "error": "owner has not finished Stripe Connect onboarding"}

    amount_cents = _amount_cents_from_match(match)
    if amount_cents < 50:  # Stripe minimum for card charges is typically $0.50
        return {"ok": False, "skipped": True,
                "error": f"amount too small for card checkout ({amount_cents}¢)"}

    period_key = _period_key(match)
    inv_no = str((match.computed_invoice or {}).get("invoice_number") or period_key)
    fee_cents = application_fee_cents(amount_cents)
    cust = (match.customer or {}).get("name") or sub.customer_name or "Offtaker"
    operator = getattr(tenant, "company_name", None) or getattr(tenant, "name", None) or "your solar provider"

    # Reuse the row for the same period+amount (idempotent re-sends). With a
    # durable link the SAME url keeps working across re-sends, and an expired
    # Session is no reason for a new row — the click re-mints it.
    existing = db.execute(
        select(OfftakerPayment).where(
            OfftakerPayment.subscription_id == sub.id,
            OfftakerPayment.period_key == period_key,
            OfftakerPayment.status.in_(("open", "paid", "expired")),
        ).order_by(OfftakerPayment.id.desc())
    ).scalars().first()
    if existing and existing.status == "paid":
        return {
            "ok": True, "already_paid": True,
            "payment_id": existing.id,
            "pay_url": existing.pay_url,
            "amount_cents": existing.amount_cents,
            "fee_cents": existing.fee_cents,
        }
    if (existing and existing.status in ("open", "expired")
            and existing.amount_cents == amount_cents
            and existing.pay_url and existing.pay_token):
        # A legacy row (no token) carries the raw Session url, which is
        # dead within a day — never hand that out again; mint fresh below.
        return {
            "ok": True, "reused": True,
            "payment_id": existing.id,
            "pay_url": existing.pay_url,
            "amount_cents": existing.amount_cents,
            "fee_cents": existing.fee_cents,
        }

    # Persist the row first so we have a stable payment_id in metadata even if
    # Stripe succeeds and the process dies before a second write.
    row = OfftakerPayment(
        tenant_id=tenant.id,
        subscription_id=sub.id,
        invoice_number=inv_no[:40],
        period_key=period_key[:40],
        amount_cents=amount_cents,
        fee_cents=fee_cents,
        currency="usd",
        status="open",
        customer_name=str(cust)[:200],
        pay_token=secrets.token_urlsafe(24),
    )
    db.add(row)
    db.flush()  # get row.id without committing yet

    period_label = ""
    ci = match.computed_invoice or {}
    if ci.get("period_start") and ci.get("period_end"):
        period_label = f"{ci['period_start']} → {ci['period_end']}"

    try:
        minted = _mint_checkout_session(
            tenant=tenant, row=row,
            customer_email=(getattr(sub, "client_email", None) or None),
            period_label=period_label)
        row.stripe_checkout_session_id = minted["id"]
        row.stripe_payment_intent_id = minted["payment_intent"]
        row.checkout_expires_at = datetime.utcfromtimestamp(minted["expires_at"])
        row.stripe_account_id = minted["account"]
        row.pay_url = durable_pay_url(tenant, row.pay_token)
        db.commit()
        # Keep the offtaker's default invoice ledger current (open row).
        try:
            from .invoice_ledger import sync_payment_into_ledger
            sync_payment_into_ledger(db, row)
        except Exception:  # noqa: BLE001
            logger.warning("ledger sync after mint failed for payment %s", row.id, exc_info=True)
        return {
            "ok": True,
            "payment_id": row.id,
            "pay_url": row.pay_url,            # durable — goes on the invoice
            "checkout_url": minted["url"],     # the Session behind it, today
            "amount_cents": amount_cents,
            "fee_cents": fee_cents,
            "session_id": minted["id"],
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("offtaker Checkout Session failed for sub=%s", sub.id)
        try:
            row.status = "failed"
            row.error = str(e)[:500]
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"ok": False, "error": f"Checkout create failed: {e}"}


def mark_payment_paid(db, *, session_dict: dict) -> dict:
    """Idempotently stamp an OfftakerPayment paid from a Checkout Session.

    On first transition to paid, returns `notify` details so the webhook can
    email the offtaker (thank-you) and the owner (funds received) without a
    second DB round-trip.
    """
    from ..models import OfftakerPayment, Tenant, BillingReportSubscription

    meta = session_dict.get("metadata") or {}
    if meta.get("kind") != "offtaker_invoice":
        return {"ignored": "not an offtaker invoice session"}

    payment_id = meta.get("payment_id")
    sess_id = session_dict.get("id")
    row = None
    if payment_id:
        try:
            row = db.get(OfftakerPayment, int(payment_id))
        except (TypeError, ValueError):
            row = None
    if row is None and sess_id:
        row = db.execute(
            select(OfftakerPayment).where(
                OfftakerPayment.stripe_checkout_session_id == sess_id)
        ).scalars().first()
    if row is None:
        return {"ignored": "offtaker payment row not found",
                "payment_id": payment_id, "session": sess_id}

    # Metadata is not sufficient: this event must identify the tracked Session.
    if not sess_id or sess_id != row.stripe_checkout_session_id:
        return {"ignored": "checkout session does not match payment", "payment_id": row.id}
    if row.status == "refunded":
        return {"ok": True, "duplicate": True, "unchanged": "refunded", "payment_id": row.id,
                "tenant": row.tenant_id}
    if row.status == "paid":
        return {"ok": True, "duplicate": True, "payment_id": row.id,
                "tenant": row.tenant_id}

    if session_dict.get("payment_status") not in ("paid", "no_payment_required"):
        # Still open / unpaid — don't flip.
        if session_dict.get("payment_status") != "paid":
            return {"ok": True, "not_paid_yet": True,
                    "payment_status": session_dict.get("payment_status"),
                    "payment_id": row.id}

    pi = session_dict.get("payment_intent")
    if isinstance(pi, dict):
        pi = pi.get("id")
    row.status = "paid"
    row.paid_at = datetime.utcnow()
    if isinstance(pi, str):
        row.stripe_payment_intent_id = pi
    # Capture the actual amount_total if Stripe adjusted (shouldn't, but honest).
    total = session_dict.get("amount_total")
    if isinstance(total, int) and total > 0:
        row.amount_cents = total
    db.commit()

    # Rebuild default ledger so "Collected $" / paid date show for this period.
    try:
        from .invoice_ledger import sync_payment_into_ledger
        sync_payment_into_ledger(db, row)
    except Exception:  # noqa: BLE001
        logger.warning("ledger sync after paid failed for payment %s", row.id, exc_info=True)

    # Snapshot notify recipients while we have the session open.
    tenant = db.get(Tenant, row.tenant_id)
    sub = db.get(BillingReportSubscription, row.subscription_id)
    offtaker_email = (
        (session_dict.get("customer_details") or {}).get("email")
        or session_dict.get("customer_email")
        or (getattr(sub, "client_email", None) if sub else None)
    )
    notify = {
        "offtaker_email": offtaker_email,
        "offtaker_name": row.customer_name or (getattr(sub, "customer_name", None) if sub else None),
        "owner_email": getattr(tenant, "contact_email", None) if tenant else None,
        "owner_name": (
            (getattr(tenant, "company_name", None) or getattr(tenant, "name", None))
            if tenant else None
        ),
        "invoice_number": row.invoice_number,
        "period_key": row.period_key,
        "amount_cents": row.amount_cents,
        "fee_cents": row.fee_cents,
        "product": getattr(tenant, "product", "array_operator") if tenant else "array_operator",
    }
    return {
        "ok": True,
        "payment_id": row.id,
        "tenant": row.tenant_id,
        "subscription_id": row.subscription_id,
        "amount_cents": row.amount_cents,
        "fee_cents": row.fee_cents,
        "notify": notify,
    }


def resolve_pay_link(db, token: str) -> dict:
    """What a click on the durable pay link should do. Never raises.

      {"action": "redirect", "url": …}        → send the offtaker to Stripe
      {"action": "paid", …}                    → already settled; show a receipt
      {"action": "unavailable", "reason": …}   → cannot take a payment right now
      {"action": "not_found"}

    A fresh open Session is reused as-is (one Stripe read); a stale, expired,
    failed or never-minted one is replaced on the SAME row, so the invoice's
    single link keeps working for as long as the invoice is open."""
    from ..models import OfftakerPayment, Tenant, BillingReportSubscription

    tok = (token or "").strip()
    if not tok or len(tok) > 64:
        return {"action": "not_found"}
    row = db.execute(
        select(OfftakerPayment).where(OfftakerPayment.pay_token == tok)
    ).scalars().first()
    if row is None:
        return {"action": "not_found"}
    tenant = db.get(Tenant, row.tenant_id)
    operator = ((getattr(tenant, "company_name", None) or getattr(tenant, "name", None))
                if tenant else None) or "your solar provider"
    base = {"payment_id": row.id, "operator": operator,
            "invoice_number": row.invoice_number, "amount_cents": row.amount_cents,
            "customer_name": row.customer_name}
    if row.status == "paid":
        return {"action": "paid", "paid_at": row.paid_at, **base}
    if row.status == "refunded":
        return {"action": "unavailable",
                "reason": "This invoice was refunded, so nothing is due on it.", **base}
    if tenant is None:
        return {"action": "not_found"}
    if not payments_enabled() or not _stripe_ready():
        return {"action": "unavailable",
                "reason": "Online payment is temporarily unavailable.", **base}
    if not connect_ready(tenant):
        try:
            create_or_get_connect_account(db, tenant)
            db.refresh(tenant)
        except Exception:  # noqa: BLE001
            pass
        if not connect_ready(tenant):
            return {"action": "unavailable",
                    "reason": f"{operator} hasn't finished setting up online payments yet.",
                    **base}

    now = datetime.utcnow()
    sid = row.stripe_checkout_session_id
    exp = row.checkout_expires_at
    # Always reconcile the last Session, even after our local expiry timestamp.
    # A completed ACH Checkout may remain unpaid for days; it is NOT expired.
    # A timeout/failed expiry gives no proof that a second charge is safe.
    if sid and row.status == "open":
        try:
            sess = stripe.checkout.Session.retrieve(sid, **_stripe_kw(row))
            sd = sess.to_dict() if hasattr(sess, "to_dict") else dict(sess)
        except Exception:  # noqa: BLE001
            logger.warning("pay-link: unable to establish Session state for %s", sid,
                           exc_info=True)
            return {"action": "unavailable",
                    "reason": "Payment status is temporarily unavailable. Please try again later.", **base}
        if sd.get("payment_status") == "paid":
            mark_payment_paid(db, session_dict=sd)
            db.refresh(row)
            if row.status == "paid":
                return {"action": "paid", "paid_at": row.paid_at, **base}
            return {"action": "unavailable", "reason": "Payment reconciliation is pending.", **base}
        if sd.get("status") == "complete":
            return {"action": "unavailable", "processing": True,
                    "reason": "Your payment is processing. Please do not pay again.", **base}
        if sd.get("status") == "open":
            if exp and (exp - now).total_seconds() > CHECKOUT_REFRESH_GRACE_SECONDS and sd.get("url"):
                return {"action": "redirect", "url": sd["url"], **base}
            try:
                stripe.checkout.Session.expire(sid, **_stripe_kw(row))
            except Exception:  # noqa: BLE001
                return {"action": "unavailable",
                        "reason": "Payment status is changing. Please try again later.", **base}
        elif sd.get("status") != "expired":
            return {"action": "unavailable",
                    "reason": "Payment status is temporarily unavailable. Please try again later.", **base}
    sub = db.get(BillingReportSubscription, row.subscription_id)
    try:
        minted = _mint_checkout_session(
            tenant=tenant, row=row,
            customer_email=(getattr(sub, "client_email", None) or None))
    except Exception as e:  # noqa: BLE001
        logger.exception("pay-link: re-mint failed for payment %s", row.id)
        row.error = f"re-mint failed: {e}"[:500]
        db.commit()
        return {"action": "unavailable",
                "reason": "Online payment is temporarily unavailable — please try "
                          "again in a few minutes.", **base}
    row.stripe_checkout_session_id = minted["id"]
    row.stripe_payment_intent_id = minted["payment_intent"]
    row.checkout_expires_at = datetime.utcfromtimestamp(minted["expires_at"])
    row.stripe_account_id = minted["account"]   # a re-mint may change charge model
    row.status = "open"
    row.error = None
    db.commit()
    return {"action": "redirect", "url": minted["url"], "reminted": True, **base}


def _row_for_session(db, session_dict: dict):
    from ..models import OfftakerPayment
    meta = session_dict.get("metadata") or {}
    if meta.get("kind") != "offtaker_invoice":
        return None
    sess_id = session_dict.get("id")
    if not sess_id:
        return None
    return db.execute(
        select(OfftakerPayment).where(
            OfftakerPayment.stripe_checkout_session_id == sess_id)
    ).scalars().first()


def mark_payment_expired(db, *, session_dict: dict) -> dict:
    """checkout.session.expired — Stripe's 24h window lapsed. Flips only the
    row whose CURRENT Session this is: a click may already have re-minted a
    newer Session on the same row, and that one must stay open. The durable
    link keeps working either way."""
    row = _row_for_session(db, session_dict)
    if row is None:
        return {"ignored": "no matching open offtaker payment",
                "session": session_dict.get("id")}
    if row.status != "open":
        return {"ok": True, "unchanged": row.status, "payment_id": row.id,
                "tenant": row.tenant_id}
    row.status = "expired"
    db.commit()
    return {"ok": True, "expired": True, "payment_id": row.id,
            "tenant": row.tenant_id, "durable": bool(row.pay_token)}


def mark_payment_async_failed(db, *, session_dict: dict) -> dict:
    """checkout.session.async_payment_failed — a delayed method (ACH debit)
    bounced after Checkout completed. The invoice is NOT paid; the row goes to
    'failed' and the durable link mints a fresh Session on the next click."""
    row = _row_for_session(db, session_dict)
    if row is None:
        return {"ignored": "no matching offtaker payment",
                "session": session_dict.get("id")}
    if row.status in ("paid", "refunded"):
        return {"ok": True, "unchanged": row.status, "payment_id": row.id,
                "tenant": row.tenant_id}
    row.status = "failed"
    row.error = "bank payment failed after checkout"
    db.commit()
    return {"ok": True, "failed": True, "payment_id": row.id, "tenant": row.tenant_id,
            "invoice_number": row.invoice_number, "amount_cents": row.amount_cents,
            "customer_name": row.customer_name, "subscription_id": row.subscription_id}


def mark_payment_refunded(db, *, charge_dict: dict) -> dict:
    """charge.refunded — money went back to the offtaker. A full refund flips
    the row to 'refunded' so the ledger and the monthly summary stop counting
    it as collected; a partial refund is noted on the row, which stays paid."""
    from ..models import OfftakerPayment
    pi = charge_dict.get("payment_intent")
    if isinstance(pi, dict):
        pi = pi.get("id")
    if not isinstance(pi, str) or not pi:
        return {"ignored": "charge has no payment_intent"}
    row = db.execute(
        select(OfftakerPayment).where(OfftakerPayment.stripe_payment_intent_id == pi)
    ).scalars().first()
    if row is None:
        return {"ignored": "no offtaker payment for this charge", "payment_intent": pi}
    try:
        refunded_cents = int(charge_dict.get("amount_refunded") or 0)
    except (TypeError, ValueError):
        refunded_cents = 0
    full = bool(charge_dict.get("refunded")) or (
        refunded_cents > 0 and refunded_cents >= int(row.amount_cents or 0))
    if full:
        row.status = "refunded"
        row.error = f"refunded {refunded_cents}¢"[:500]
    else:
        row.error = f"partially refunded {refunded_cents}¢ of {row.amount_cents}¢"[:500]
    db.commit()
    try:
        from .invoice_ledger import sync_payment_into_ledger
        sync_payment_into_ledger(db, row)
    except Exception:  # noqa: BLE001
        logger.warning("ledger sync after refund failed for payment %s", row.id,
                       exc_info=True)
    return {"ok": True, "refunded": full, "refunded_cents": refunded_cents,
            "payment_id": row.id, "tenant": row.tenant_id,
            "invoice_number": row.invoice_number, "amount_cents": row.amount_cents,
            "customer_name": row.customer_name}


def send_payment_received_emails(notify: dict) -> dict:
    """Thank the offtaker + notify the owner that an invoice was paid online.

    Best-effort: never raises. Stripe Checkout already shows a receipt page;
    these emails are the branded Array Operator confirmation.
    """
    if not notify:
        return {"sent": False}
    from ..notify import _send_via_resend
    from ..email_skin import render_email_skin, render_email_skin_text
    from ..branding import app_url, brand_name

    amt = (notify.get("amount_cents") or 0) / 100.0
    fee = (notify.get("fee_cents") or 0) / 100.0
    net = max(amt - fee, 0.0)
    amt_s = f"${amt:,.2f}"
    inv = notify.get("invoice_number") or ""
    period = notify.get("period_key") or ""
    owner = notify.get("owner_name") or "your solar provider"
    offtaker = notify.get("offtaker_name") or "there"
    product = notify.get("product") or "array_operator"
    dash = app_url(product).rstrip("/") + "/#reports"
    brand = brand_name(product)
    sent = {"offtaker": False, "owner": False}

    # ── offtaker thank-you ────────────────────────────────────────────────
    to_off = (notify.get("offtaker_email") or "").strip()
    if to_off and "@" in to_off:
        body_html = (
            f"<p>Hi { _esc(offtaker.split()[0] if offtaker else 'there') },</p>"
            f"<p>We received your payment of <b>{amt_s}</b>"
            f"{f' for invoice { _esc(inv)}' if inv else ''}."
            f" Thank you — you're all set.</p>"
            f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin:12px 0;">'
            f'<tr><td style="padding:6px 0;opacity:.65;">Amount paid</td>'
            f'<td style="padding:6px 0;text-align:right;font-weight:700;color:#047857;">{amt_s}</td></tr>'
            + (f'<tr><td style="padding:6px 0;opacity:.65;">Invoice</td>'
               f'<td style="padding:6px 0;text-align:right;">{_esc(inv)}</td></tr>' if inv else "")
            + (f'<tr><td style="padding:6px 0;opacity:.65;">Period</td>'
               f'<td style="padding:6px 0;text-align:right;">{_esc(period)}</td></tr>' if period else "")
            + f"</table>"
            f"<p style=\"font-size:13px;opacity:.7;\">Questions? Reply to this email and it goes to { _esc(owner) }.</p>"
        )
        html = render_email_skin(
            preheader=f"Payment received — {amt_s}",
            headline="Payment received",
            intro_line=f"Thank you · {owner}",
            body_html=body_html,
            footer_line=f"Solar credit invoice from {owner}.",
            wordmark=owner,
            product=product,
        )
        text = render_email_skin_text(
            headline="Payment received",
            intro_line=f"Thank you · {owner}",
            body_text=(
                f"Hi {offtaker},\n\n"
                f"We received your payment of {amt_s}"
                f"{f' for invoice {inv}' if inv else ''}. Thank you — you're all set.\n\n"
                f"Questions? Reply to this email and it goes to {owner}."
            ),
            wordmark=owner,
            product=product,
        )
        try:
            sent["offtaker"] = bool(_send_via_resend(
                to=to_off,
                subject=f"Payment received — {amt_s}" + (f" · invoice {inv}" if inv else ""),
                html=html, text=text, product=product,
            ))
        except Exception:  # noqa: BLE001
            logger.exception("offtaker payment thank-you email failed")

    # ── owner notice ──────────────────────────────────────────────────────
    to_own = (notify.get("owner_email") or "").strip()
    if to_own and "@" in to_own:
        body_html = (
            f"<p><b>{_esc(offtaker)}</b> just paid their solar credit invoice online.</p>"
            f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin:12px 0;">'
            f'<tr><td style="padding:6px 0;opacity:.65;">Amount paid</td>'
            f'<td style="padding:6px 0;text-align:right;font-weight:700;color:#047857;">{amt_s}</td></tr>'
            f'<tr><td style="padding:6px 0;opacity:.65;">Your net (after {fee_bps()/100:.2g}% fee)</td>'
            f'<td style="padding:6px 0;text-align:right;">${net:,.2f}</td></tr>'
            + (f'<tr><td style="padding:6px 0;opacity:.65;">Invoice</td>'
               f'<td style="padding:6px 0;text-align:right;">{_esc(inv)}</td></tr>' if inv else "")
            + f"</table>"
            f'<p style="margin-top:14px;"><a href="{dash}" '
            f'style="background:#047857;color:#fff;padding:11px 18px;border-radius:8px;'
            f'text-decoration:none;font-weight:600;display:inline-block;">Open Reports</a></p>'
            f'<p style="font-size:13px;opacity:.7;">Funds land in your connected bank per Stripe\'s payout schedule.</p>'
        )
        html = render_email_skin(
            preheader=f"{offtaker} paid {amt_s}",
            headline="Payment received",
            intro_line=f"{offtaker} · {amt_s}",
            body_html=body_html,
            footer_line=f"{brand} · offtaker payments",
            product=product,
        )
        text = render_email_skin_text(
            headline="Payment received",
            intro_line=f"{offtaker} · {amt_s}",
            body_text=(
                f"{offtaker} just paid their solar credit invoice online.\n\n"
                f"Amount paid: {amt_s}\n"
                f"Your net (after {fee_bps()/100:.2g}% fee): ${net:,.2f}\n"
                + (f"Invoice: {inv}\n" if inv else "")
                + f"\nOpen Reports: {dash}\n"
                f"Funds land in your connected bank per Stripe's payout schedule."
            ),
            product=product,
        )
        try:
            sent["owner"] = bool(_send_via_resend(
                to=to_own,
                subject=f"Paid · {offtaker} · {amt_s}",
                html=html, text=text, product=product,
            ))
        except Exception:  # noqa: BLE001
            logger.exception("owner payment-received email failed")

    return {"sent": bool(sent["offtaker"] or sent["owner"]), **sent}


def _esc(s: str) -> str:
    import html as _html
    return _html.escape(str(s or ""))


def sync_connect_from_account_event(db, account: dict) -> dict:
    """account.updated webhook → flip Tenant.stripe_connect_charges_enabled."""
    from ..models import Tenant

    acct_id = account.get("id")
    if not acct_id:
        return {"ignored": "no account id"}
    t = db.execute(
        select(Tenant).where(Tenant.stripe_connect_account_id == acct_id)
    ).scalars().first()
    if not t:
        return {"ignored": f"no tenant for connect account {acct_id}"}
    enabled = bool(account.get("charges_enabled"))
    old = bool(t.stripe_connect_charges_enabled)
    t.stripe_connect_charges_enabled = enabled
    db.commit()
    return {
        "ok": True,
        "tenant": t.id,
        "charges_enabled": enabled,
        "changed": old != enabled,
    }
