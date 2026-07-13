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
from datetime import datetime
from typing import Any, Optional

import stripe
from sqlalchemy import select

logger = logging.getLogger(__name__)

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
    Express account on our platform (by metadata.tenant_id or contact email)
    and attach it. Fixes the 'I finished Stripe KYC but pay links never mint'
    case when the owner set up Connect under a different session/tenant row
    or the DB write didn't land on the tenant they're sending from.
    """
    if getattr(tenant, "stripe_connect_account_id", None):
        return {"ok": True, "linked": False, "account_id": tenant.stripe_connect_account_id}
    if not _stripe_ready():
        return {"ok": False, "error": "Stripe not configured"}
    tid = str(getattr(tenant, "id", "") or "")
    email = (getattr(tenant, "contact_email", None) or "").strip().lower()
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
                a_email = (ad.get("email") or "").strip().lower()
                if tid and meta.get("tenant_id") == tid:
                    matched = ad
                    break
                if email and a_email and a_email == email:
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

    # Reuse an open session for the same period+amount (idempotent re-sends).
    if not force:
        existing = db.execute(
            select(OfftakerPayment).where(
                OfftakerPayment.subscription_id == sub.id,
                OfftakerPayment.period_key == period_key,
                OfftakerPayment.status.in_(("open", "paid")),
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
        if (existing and existing.status == "open"
                and existing.amount_cents == amount_cents
                and existing.pay_url):
            return {
                "ok": True, "reused": True,
                "payment_id": existing.id,
                "pay_url": existing.pay_url,
                "amount_cents": existing.amount_cents,
                "fee_cents": existing.fee_cents,
            }

    from ..branding import app_url
    base = app_url(getattr(tenant, "product", "array_operator")).rstrip("/")
    success_url = f"{base}/?paid=1#reports"
    cancel_url = f"{base}/?paid=0#reports"

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
    )
    db.add(row)
    db.flush()  # get row.id without committing yet

    meta = {
        "kind": "offtaker_invoice",
        "tenant_id": str(tenant.id),
        "subscription_id": str(sub.id),
        "payment_id": str(row.id),
        "invoice_number": inv_no[:40],
        "period_key": period_key[:40],
    }
    period_label = ""
    ci = match.computed_invoice or {}
    if ci.get("period_start") and ci.get("period_end"):
        period_label = f"{ci['period_start']} → {ci['period_end']}"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=success_url + "&session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
            customer_email=(getattr(sub, "client_email", None) or None),
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount_cents,
                    "product_data": {
                        "name": f"Solar credit invoice {inv_no}",
                        "description": (
                            f"{cust} · {period_label or period_key} · payable to {operator}"
                        )[:500],
                    },
                },
            }],
            payment_intent_data={
                "application_fee_amount": fee_cents,
                "transfer_data": {
                    "destination": tenant.stripe_connect_account_id,
                },
                "metadata": meta,
                "description": f"Solar credit · {cust} · {inv_no}"[:500],
            },
            metadata=meta,
            # Stripe Checkout requires expires_at < 24h from creation
            # (prod log 2026-07-13: 30-day expires_at → pay links never minted).
            # Use time.time() NOT datetime.utcnow().timestamp() — the latter
            # treats naive UTC as local time and can land >24h out on non-UTC hosts.
            expires_at=int(__import__("time").time()) + 23 * 3600,
        )
        sess_id = session["id"] if isinstance(session, dict) else session.id
        pay_url = session["url"] if isinstance(session, dict) else session.url
        pi = None
        if isinstance(session, dict):
            pi = session.get("payment_intent")
        else:
            pi = getattr(session, "payment_intent", None)
        if isinstance(pi, dict):
            pi = pi.get("id")

        row.stripe_checkout_session_id = sess_id
        row.stripe_payment_intent_id = pi if isinstance(pi, str) else None
        row.pay_url = pay_url
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
            "pay_url": pay_url,
            "amount_cents": amount_cents,
            "fee_cents": fee_cents,
            "session_id": sess_id,
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

    if row.status == "paid":
        return {"ok": True, "duplicate": True, "payment_id": row.id,
                "tenant": row.tenant_id}

    if session_dict.get("payment_status") not in (None, "paid", "no_payment_required"):
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
