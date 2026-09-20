"""Mail room — every off-taker invoice going out, and every one that went out.

LEFT ("going out"): what will reach an off-taker and when — drafts awaiting the
operator, frozen obligations that are held / retrying / unconfirmed, and the
subscriptions the scheduler will pick up on its next run.

RIGHT ("sent"): one entry per issued invoice, read from the frozen obligation
(`OfftakerInvoice`: the exact figures that were billed), the dispatch row
(`BillingEmailDispatch`: the exact email payload + Resend receipt), the pay
link (`OfftakerPayment`) and any offline receipts (`OfftakerSettlement`).
Nothing here recomputes an invoice — the mail room is a record of what went
out, not an estimate of what would go out today. Invoices issued before the
frozen table existed are synthesised from the subscription's last-send stamps
and the approval-inbox rows, flagged `legacy` so the gap in evidence is honest.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

SENT_STATUSES = ("accepted", "uncertain")
OPEN_STATUSES = ("prepared", "held", "sending", "failed")
DELIVERY_UNCONFIRMED = "unconfirmed"


# ── schedule helpers (mirror api/scheduler.py: 1st of the period, 09:00 UTC) ──

def next_month_first(now: datetime) -> datetime:
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    cand = datetime(y, m, 1, 9, 0)
    if now.day == 1 and now.hour < 9:
        return datetime(now.year, now.month, 1, 9, 0)
    return cand


def next_quarter_first(now: datetime) -> datetime:
    for m in (1, 4, 7, 10, 13):
        ny, nm = (now.year, m) if m <= 12 else (now.year + 1, 1)
        cand = datetime(ny, nm, 1, 9, 0)
        if cand > now:
            return cand
    return datetime(now.year + 1, 1, 1, 9, 0)


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat() + ("Z" if v.tzinfo is None else "")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _money(cents) -> Optional[float]:
    return None if cents is None else round(int(cents) / 100.0, 2)


def _period_label(start, end) -> Optional[str]:
    if not start and not end:
        return None
    return f"{_iso(start) or '—'} → {_iso(end) or '—'}"


def _day(dt: datetime) -> str:
    """'Oct 1' without a platform-specific strftime directive."""
    return f"{dt:%b} {dt.day}"


def _as_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    return [str(x) for x in v if x]


# ── subscription context (one pass, no N+1) ────────────────────────────────

def sub_context(db, tenant_id: str) -> dict[int, dict]:
    """Everything the mail room says about an off-taker that is NOT per-send:
    who they are, how they are reached, which utility account and array(s)
    their invoice is computed from, and the pricing knobs on the row."""
    from ..models import BillingReportSubscription, UtilityAccount, Array

    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
    ).scalars().all()
    ua_ids = {s.utility_account_id for s in subs if s.utility_account_id}
    arr_ids = {s.array_id for s in subs if s.array_id}
    for s in subs:
        for a in (s.array_allocations or []):
            try:
                arr_ids.add(int(a.get("array_id")))
            except (TypeError, ValueError, AttributeError):
                pass
    uas = {}
    if ua_ids:
        for ua in db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tenant_id, UtilityAccount.id.in_(ua_ids))).scalars():
            uas[ua.id] = ua
            if ua.array_id:
                arr_ids.add(ua.array_id)
    arrays = {}
    if arr_ids:
        for a in db.execute(select(Array).where(Array.tenant_id == tenant_id, Array.id.in_(arr_ids))).scalars():
            arrays[a.id] = a

    out: dict[int, dict] = {}
    for s in subs:
        ua = uas.get(s.utility_account_id) if s.utility_account_id else None
        names = []
        if s.array_allocations:
            for a in s.array_allocations:
                try:
                    arr = arrays.get(int(a.get("array_id")))
                except (TypeError, ValueError, AttributeError):
                    arr = None
                if arr is not None:
                    names.append(arr.name)
        elif s.array_id and arrays.get(s.array_id) is not None:
            names.append(arrays[s.array_id].name)
        elif ua is not None and ua.array_id and arrays.get(ua.array_id) is not None:
            names.append(arrays[ua.array_id].name)
        out[s.id] = {
            "subscription_id": s.id,
            "customer_name": s.customer_name,
            "client_email": s.client_email,
            "cc_emails": _as_list(s.cc_emails),
            "operator_email": s.operator_email,
            "send_mode": s.send_mode or "to_me",
            "delivery_mode": s.delivery_mode or "approval",
            "cadence": s.cadence or "monthly",
            "enabled": bool(s.enabled),
            "allocation_pct": s.allocation_pct,
            "array_share_pct": s.array_share_pct,
            "rate_per_kwh": s.rate_per_kwh,
            "net_rate_per_kwh": s.net_rate_per_kwh,
            "net_rate_adder_per_kwh": s.net_rate_adder_per_kwh,
            "discount_pct": s.discount_pct,
            "budget_amount_usd": s.budget_amount_usd,
            "pending_credit_usd": s.pending_credit_usd,
            "last_sent_at": _iso(s.last_sent_at),
            "last_sent_period_end": s.last_sent_period_end,
            "last_sent_amount_usd": s.last_sent_amount_usd,
            "last_invoice_number": s.last_invoice_number,
            "last_resend_email_id": s.last_resend_email_id,
            "last_delivered_at": _iso(s.last_delivered_at),
            "last_bounced_at": _iso(s.last_bounced_at),
            "last_bounce_reason": s.last_bounce_reason,
            "next_send_at": _iso(s.next_send_at),
            "utility": ({
                "utility_account_id": ua.id,
                "provider": ua.provider,
                "account_number": ua.account_number,
                "nickname": ua.nickname,
            } if ua is not None else None),
            "arrays": names,
            "_row": s,
        }
    return out


def _utility_view(ctx: dict) -> Optional[dict]:
    u = ctx.get("utility")
    return None if not u else {k: v for k, v in u.items()}


# ── delivery truth by Resend id ────────────────────────────────────────────

def _delivery_events(db, resend_ids: set[str]) -> dict[str, dict]:
    """{resend_id: {status, delivered_at, bounced_at, reason}} from the Resend
    webhook receipts; absent id → nothing known beyond 'accepted by mailer'."""
    out: dict[str, dict] = {}
    if not resend_ids:
        return out
    try:
        from ..energy_agent import EaEmailDelivery  # receipts table (self-created)
    except Exception:  # noqa: BLE001
        return out
    try:
        # Missing legacy receipt tables must not poison a PostgreSQL transaction.
        with db.begin_nested():
            rows = db.execute(
                select(EaEmailDelivery).where(EaEmailDelivery.resend_email_id.in_(resend_ids))
                .order_by(EaEmailDelivery.created_at, EaEmailDelivery.id)
            ).scalars().all()
    except Exception:  # noqa: BLE001
        return out
    for r in rows:
        ev = (getattr(r, "event", "") or "").lower()
        rid = getattr(r, "resend_email_id", None)
        if not rid:
            continue
        d = out.setdefault(rid, {"status": None, "delivered_at": None,
                                 "bounced_at": None, "reason": None})
        if ev in ("delivered", "email.delivered"):
            d["delivered_at"] = _iso(r.created_at)
            d["status"] = d["status"] or "delivered"
        elif ev in ("bounced", "complained", "email.bounced", "email.complained"):
            d["bounced_at"] = _iso(r.created_at)
            d["reason"] = getattr(r, "reason", None)
            d["status"] = "bounced"
    return out


# ── the SENT side ──────────────────────────────────────────────────────────

def sent_items(db, tenant_id: str, *, limit: int = 300, offset: int = 0,
               ctx: Optional[dict[int, dict]] = None) -> tuple[list[dict], int]:
    """Issued invoices, newest first, each with figures + envelope + delivery +
    payment. Returns (items, total_count)."""
    from ..models import (OfftakerInvoice, BillingEmailDispatch, OfftakerPayment,
                          OfftakerSettlement)
    from sqlalchemy import func

    ctx = ctx if ctx is not None else sub_context(db, tenant_id)
    total = db.execute(
        select(func.count(OfftakerInvoice.id)).where(
            OfftakerInvoice.tenant_id == tenant_id,
            OfftakerInvoice.status.in_(SENT_STATUSES))
    ).scalar() or 0
    # Fix the narrow ID page first: a concurrent accepted invoice must not
    # shift OFFSET between batches and produce duplicate audit findings.
    ids = db.scalars(select(OfftakerInvoice.id).where(
        OfftakerInvoice.tenant_id == tenant_id, OfftakerInvoice.status.in_(SENT_STATUSES))
        .order_by(OfftakerInvoice.sent_at.desc().nullslast(), OfftakerInvoice.id.desc())
        .offset(max(0, offset)).limit(max(1, min(limit, 1000)))).all()
    items = []
    for batch_offset in range(0, len(ids), 50):
        items.extend(_sent_items_page(db, tenant_id, ctx, ids[batch_offset:batch_offset + 50]))
    return items, int(total)


def _sent_items_page(db, tenant_id, ctx, invoice_ids):
    """Keep heavyweight frozen HTML/PDF ORM objects inside one bounded scope.

    Only small rendered summary dicts escape. SQLAlchemy's weak identity map
    releases unmodified rows when this scope returns, without detaching objects
    a caller may already own or discarding caller changes.
    """
    from ..models import (OfftakerInvoice, BillingEmailDispatch, OfftakerPayment,
                          OfftakerSettlement)
    from sqlalchemy import func
    rows = db.execute(
        select(OfftakerInvoice).where(
            OfftakerInvoice.tenant_id == tenant_id, OfftakerInvoice.id.in_(invoice_ids))
        .order_by(OfftakerInvoice.sent_at.desc().nullslast(), OfftakerInvoice.id.desc())
    ).scalars().all()
    if not rows:
        return []

    ids = [r.id for r in rows]
    dispatches = {d.key: d for d in db.execute(
        select(BillingEmailDispatch).where(
            BillingEmailDispatch.tenant_id == tenant_id,
            BillingEmailDispatch.key.in_([f"invoice:{i}" for i in ids]))
    ).scalars()}
    pay_ids = [r.payment_id for r in rows if r.payment_id]
    payments = {}
    if pay_ids:
        payments = {p.id: p for p in db.execute(
            select(OfftakerPayment).where(OfftakerPayment.tenant_id == tenant_id, OfftakerPayment.id.in_(pay_ids))).scalars()}
    settled: dict[int, int] = {}
    for inv_id, cents in db.execute(
            select(OfftakerSettlement.invoice_id, func.sum(OfftakerSettlement.amount_cents))
            .where(OfftakerSettlement.tenant_id == tenant_id,
                   OfftakerSettlement.invoice_id.in_(ids))
            .group_by(OfftakerSettlement.invoice_id)).all():
        settled[int(inv_id)] = int(cents or 0)
    events = _delivery_events(db, {d.resend_email_id for d in dispatches.values()
                                   if d.resend_email_id})

    items = []
    for r in rows:
        items.append(_sent_item(r, dispatches.get(f"invoice:{r.id}"),
                                payments.get(r.payment_id) if r.payment_id else None,
                                settled.get(r.id, 0), events, ctx.get(r.subscription_id) or {}))
    return items


def _sent_item(inv, dispatch, payment, settled_cents: int, events: dict, c: dict) -> dict:
    snap = inv.snapshot or {}
    ci = snap.get("computed_invoice") or {}
    rs = inv.render_snapshot or {}
    variant = ((rs.get("variants") or {}).get("online")
               or (rs.get("variants") or {}).get("offline") or {})
    env = dict(dispatch.email) if dispatch is not None and isinstance(dispatch.email, dict) else variant
    to = _as_list(env.get("to"))
    cc = _as_list(env.get("cc"))
    bcc = _as_list(env.get("bcc"))
    attachments = [a.get("filename") for a in (rs.get("attachments") or []) if a.get("filename")]
    if not attachments:
        attachments = [a.get("filename") for a in (env.get("attachments") or [])
                       if isinstance(a, dict) and a.get("filename")]

    resend_id = dispatch.resend_email_id if dispatch is not None else None
    ev = events.get(resend_id) if resend_id else None
    if ev and ev.get("status"):
        delivery = {"status": ev["status"], "delivered_at": ev.get("delivered_at"),
                    "bounced_at": ev.get("bounced_at"), "reason": ev.get("reason")}
    elif resend_id and c.get("last_resend_email_id") == resend_id and c.get("last_bounced_at"):
        delivery = {"status": "bounced", "delivered_at": None,
                    "bounced_at": c.get("last_bounced_at"), "reason": c.get("last_bounce_reason")}
    elif resend_id and c.get("last_resend_email_id") == resend_id and c.get("last_delivered_at"):
        delivery = {"status": "delivered", "delivered_at": c.get("last_delivered_at"),
                    "bounced_at": None, "reason": None}
    elif inv.status == "uncertain" or (dispatch is not None and dispatch.status in ("sending", "uncertain")):
        delivery = {"status": DELIVERY_UNCONFIRMED, "delivered_at": None, "bounced_at": None,
                    "reason": (dispatch.error if dispatch is not None else None) or inv.last_error}
    else:
        delivery = {"status": "accepted", "delivered_at": None, "bounced_at": None, "reason": None}

    # Money truth mirrors invoice_ledger.invoice_balance: a refund never
    # reopens the debt, the platform fee never counts against the off-taker,
    # and an online payment only counts once Stripe said paid.
    amount = _money(inv.amount_cents)
    pay = None
    online_gross = 0
    refunded = 0
    if payment is not None:
        settled = payment.status in ("paid", "refunded")
        online_gross = int(payment.amount_cents or 0) if settled else 0
        refunded = int(getattr(payment, "refunded_cents", 0) or 0) if settled else 0
        pay = {"payment_id": payment.id, "status": payment.status,
               "paid_at": _iso(payment.paid_at),
               "amount_usd": _money(payment.amount_cents),
               "fee_usd": _money(payment.fee_cents),
               "refunded_usd": _money(refunded),
               "pay_url": payment.pay_url if payment.status in ("open", "expired") else None}
    gross = online_gross + int(settled_cents or 0)
    offline_usd = _money(settled_cents) or 0.0
    paid_usd = _money(gross) or 0.0
    outstanding = None if inv.amount_cents is None else _money(max(int(inv.amount_cents) - gross, 0))
    if pay is None and not settled_cents:
        pay_summary = "not_tracked"
    elif refunded and refunded >= gross:
        pay_summary = "refunded"
    elif outstanding is not None and outstanding <= 0.005:
        pay_summary = "paid"
    elif gross > 0:
        pay_summary = "partial"
    else:
        pay_summary = "unpaid"

    rate = {
        "net_rate_per_kwh": ci.get("net_rate_per_kwh"),
        "effective_rate_per_kwh": ci.get("effective_rate_per_kwh") or ci.get("rate_per_kwh"),
        "adder_per_kwh": ci.get("adder_per_kwh"),
        "discount_pct": ci.get("discount_pct"),
        "source": ci.get("net_rate_source") or ci.get("rate_source"),
        "note": ci.get("net_rate_note"),
        "operator_entered": ci.get("rate_is_operator_entered"),
    }
    return {
        "id": inv.id,
        "legacy": False,
        "kind": "trueup" if (ci.get("is_trueup") or str(inv.period_key).startswith("trueup:")) else "invoice",
        "subscription_id": inv.subscription_id,
        "customer_name": c.get("customer_name") or snap.get("customer_name") or (snap.get("customer") or {}).get("name"),
        "invoice_number": inv.invoice_number or ci.get("invoice_number"),
        "status": inv.status,
        "sent_at": _iso(inv.sent_at),
        "period_key": inv.period_key,
        "period_label": _period_label(inv.period_start, inv.period_end),
        "period_start": _iso(inv.period_start),
        "period_end": _iso(inv.period_end),
        "amount_usd": amount,
        "credit_applied_usd": _money(inv.credit_applied_cents),
        "kwh": inv.customer_kwh if inv.customer_kwh is not None else ci.get("kwh"),
        "array_kwh": ci.get("project_total_kwh") or ci.get("array_kwh"),
        "allocation_pct": ci.get("allocation_pct") or snap.get("allocation_pct") or c.get("allocation_pct"),
        "array_share_pct": ci.get("array_share_pct") or c.get("array_share_pct"),
        "rate": rate,
        "kwh_source": ci.get("kwh_source"),
        "billing_basis": ci.get("billing_basis"),
        "has_utility_bill": ci.get("has_utility_bill"),
        "utility": _utility_view(c),
        "arrays": c.get("arrays") or [],
        "to": to, "cc": cc, "bcc": bcc,
        "from_addr": env.get("from_addr"),
        "reply_to": env.get("reply_to"),
        "subject": env.get("subject"),
        "attachments": attachments,
        "dispatch": ({"status": dispatch.status, "attempts": dispatch.attempts,
                      "resend_email_id": resend_id, "error": dispatch.error,
                      "retry_at": _iso(dispatch.retry_at)} if dispatch is not None else None),
        "delivery": delivery,
        "payment": pay,
        "offline_settled_usd": offline_usd,
        "paid_usd": paid_usd,
        "refunded_usd": _money(refunded),
        "outstanding_usd": outstanding,
        "payment_summary": pay_summary,
        "warnings": snap.get("warnings") or [],
        "send_mode": c.get("send_mode"),
        "delivery_mode": c.get("delivery_mode"),
        "cadence": c.get("cadence"),
    }


def known_frozen_periods(db, tenant_id):
    """Suppress legacy duplicates against all history, independent of pagination."""
    from ..models import OfftakerInvoice
    return {(sid, str(end or key or "")[:7]) for sid, end, key in db.execute(
        select(OfftakerInvoice.subscription_id, OfftakerInvoice.period_end, OfftakerInvoice.period_key)
        .where(OfftakerInvoice.tenant_id == tenant_id, OfftakerInvoice.status.in_(SENT_STATUSES)))}


def legacy_items(db, tenant_id: str, ctx: dict[int, dict], known_periods: set[tuple[int, str]]) -> list[dict]:
    """Sends that predate the frozen table: the approval inbox's 'sent' rows
    (one per period) plus each subscription's last-send stamp. No envelope, no
    evidence — flagged so the gap is visible instead of silently filled."""
    from ..models import ReportDraft
    items: list[dict] = []
    seen: set[tuple[int, str]] = set(known_periods)
    drafts = db.execute(
        select(ReportDraft).where(ReportDraft.tenant_id == tenant_id,
                                  ReportDraft.status == "sent")
        .order_by(ReportDraft.sent_at.desc().nullslast(), ReportDraft.id.desc())
        .limit(500)
    ).scalars().all()
    for d in drafts:
        key = (d.subscription_id, (d.period_label or "")[:7] or f"draft:{d.id}")
        if key in seen:
            continue
        seen.add(key)
        c = ctx.get(d.subscription_id) or {}
        items.append(_legacy_item(f"legacy:draft:{d.id}", d.subscription_id, c,
                                  invoice_number=d.invoice_number, amount=d.amount_usd,
                                  kwh=d.customer_kwh, period_label=d.period_label,
                                  sent_at=d.sent_at or d.created_at))
    for sid, c in ctx.items():
        if not c.get("last_sent_at"):
            continue
        pe = c.get("last_sent_period_end") or ""
        key = (sid, str(pe)[:7] or f"sub:{sid}")
        if key in seen or any(k[0] == sid and k[1] == str(pe)[:7] for k in seen):
            continue
        seen.add(key)
        items.append(_legacy_item(f"legacy:sub:{sid}", sid, c,
                                  invoice_number=c.get("last_invoice_number"),
                                  amount=c.get("last_sent_amount_usd"), kwh=None,
                                  period_label=pe or None, sent_at=c.get("last_sent_at")))
    items.sort(key=lambda x: x.get("sent_at") or "", reverse=True)
    return items


def _legacy_item(item_id: str, sid: int, c: dict, *, invoice_number, amount, kwh,
                 period_label, sent_at) -> dict:
    delivery = {"status": "accepted", "delivered_at": None, "bounced_at": None, "reason": None}
    if c.get("last_bounced_at"):
        delivery = {"status": "bounced", "delivered_at": None,
                    "bounced_at": c["last_bounced_at"], "reason": c.get("last_bounce_reason")}
    elif c.get("last_delivered_at"):
        delivery = {"status": "delivered", "delivered_at": c["last_delivered_at"],
                    "bounced_at": None, "reason": None}
    return {
        "id": item_id, "legacy": True, "kind": "invoice",
        "subscription_id": sid,
        "customer_name": c.get("customer_name"),
        "invoice_number": invoice_number, "status": "accepted",
        "sent_at": _iso(sent_at) if not isinstance(sent_at, str) else sent_at,
        "period_key": None, "period_label": period_label,
        "period_start": None, "period_end": None,
        "amount_usd": amount, "credit_applied_usd": None, "kwh": kwh,
        "array_kwh": None, "allocation_pct": c.get("allocation_pct"),
        "array_share_pct": c.get("array_share_pct"),
        "rate": {"net_rate_per_kwh": c.get("net_rate_per_kwh"), "effective_rate_per_kwh": None,
                 "adder_per_kwh": c.get("net_rate_adder_per_kwh"), "discount_pct": c.get("discount_pct"),
                 "source": None, "note": "issued before frozen evidence existed",
                 "operator_entered": None},
        "kwh_source": None, "billing_basis": None, "has_utility_bill": None,
        "utility": _utility_view(c), "arrays": c.get("arrays") or [],
        "to": [c["client_email"]] if c.get("send_mode") in ("to_client", "to_both") and c.get("client_email") else [],
        "cc": c.get("cc_emails") or [], "bcc": [], "from_addr": None, "reply_to": None,
        "subject": None, "attachments": [], "dispatch": None,
        "delivery": delivery, "payment": None, "offline_settled_usd": 0.0,
        "paid_usd": 0.0, "outstanding_usd": None, "payment_summary": "not_tracked",
        "warnings": [], "send_mode": c.get("send_mode"),
        "delivery_mode": c.get("delivery_mode"), "cadence": c.get("cadence"),
    }


# ── the GOING-OUT side ─────────────────────────────────────────────────────

def outgoing_items(db, tenant_id: str, tenant, ctx: Optional[dict[int, dict]] = None,
                   now: Optional[datetime] = None) -> list[dict]:
    from ..models import ReportDraft, OfftakerInvoice, BillingEmailDispatch

    ctx = ctx if ctx is not None else sub_context(db, tenant_id)
    now = now or datetime.utcnow()
    paused = bool(getattr(tenant, "sending_paused", False))
    items: list[dict] = []
    covered: set[int] = set()

    # 1. Drafts awaiting the operator.
    drafts = db.execute(
        select(ReportDraft).where(ReportDraft.tenant_id == tenant_id,
                                  ReportDraft.status == "pending")
        .order_by(ReportDraft.created_at.desc())
    ).scalars().all()
    for d in drafts:
        c = ctx.get(d.subscription_id) or {}
        covered.add(d.subscription_id)
        items.append({
            "kind": "draft", "draft_id": d.id, "invoice_id": None,
            "subscription_id": d.subscription_id,
            "customer_name": d.customer_name or c.get("customer_name"),
            "email": c.get("client_email"), "send_mode": c.get("send_mode"),
            "delivery_mode": c.get("delivery_mode"), "cadence": c.get("cadence"),
            "period_label": d.period_label, "amount_usd": d.amount_usd,
            "kwh": d.customer_kwh, "invoice_number": d.invoice_number,
            "utility": _utility_view(c), "arrays": c.get("arrays") or [],
            "when": None, "when_label": "On your approval",
            "status": "awaiting_approval", "reason": None,
            "created_at": _iso(d.created_at), "sort": 0,
        })

    # 2. Frozen obligations not yet accepted: held / retrying / unconfirmed.
    open_rows = db.execute(
        select(OfftakerInvoice).where(OfftakerInvoice.tenant_id == tenant_id,
                                      OfftakerInvoice.status.in_(OPEN_STATUSES))
        .order_by(OfftakerInvoice.period_end.desc().nullslast())
    ).scalars().all()
    keys = [f"invoice:{r.id}" for r in open_rows]
    dispatches = {d.key: d for d in db.execute(
        select(BillingEmailDispatch).where(BillingEmailDispatch.tenant_id == tenant_id,
                                           BillingEmailDispatch.key.in_(keys))
    ).scalars()} if keys else {}
    for r in open_rows:
        c = ctx.get(r.subscription_id) or {}
        covered.add(r.subscription_id)
        d = dispatches.get(f"invoice:{r.id}")
        ci = (r.snapshot or {}).get("computed_invoice") or {}
        if d is not None and d.status in ("sending", "uncertain") or r.status == "sending":
            kind, status, when, label = "unconfirmed", "unconfirmed", None, "Delivery needs verification"
        elif d is not None and d.status == "accepted":
            kind, status, when, label = "prepared", "accepted_pending_record", None, "Accepted; local record needs recovery"
        elif paused or not c.get("enabled", True):
            kind, status, when = "held", "paused" if paused else "disabled", None
            label = "Sending paused" if paused else "Off-taker disabled; no automatic retry"
        elif d is not None and (d.attempts or 0) >= 8 and d.status == "failed":
            kind, status, when, label = "held", "retry_exhausted", None, "Automatic retries exhausted"
        elif r.status == "held":
            kind, status, when, label = "held", "held", None, "Held"
        elif r.status == "failed" or (d is not None and d.status == "failed"):
            kind, status = "retrying", "retrying"
            when = d.retry_at if d is not None else None
            label = f"Retry after {_day(when)} {when:%H:%M} UTC" if when else "Retry pending"
        else:
            kind, status, when = "prepared", "prepared", None
            label = "Frozen, waiting for the send step"
        items.append({
            "kind": kind, "draft_id": None, "invoice_id": r.id,
            "subscription_id": r.subscription_id,
            "customer_name": c.get("customer_name"),
            "email": c.get("client_email"), "send_mode": c.get("send_mode"),
            "delivery_mode": c.get("delivery_mode"), "cadence": c.get("cadence"),
            "period_label": _period_label(r.period_start, r.period_end) or r.period_key,
            "amount_usd": _money(r.amount_cents) if r.snapshot else None,
            "kwh": r.customer_kwh if r.customer_kwh is not None else ci.get("kwh"),
            "invoice_number": r.invoice_number or None,
            "utility": _utility_view(c), "arrays": c.get("arrays") or [],
            "when": _iso(when), "when_label": label, "status": status,
            "reason": r.last_error or (d.error if d is not None else None),
            "created_at": _iso(r.created_at), "sort": 1,
        })

    # 3. Everyone else the scheduler will pick up on its next run.
    for sid, c in ctx.items():
        if sid in covered or not c.get("enabled", True):
            continue
        cadence = c.get("cadence") or "monthly"
        fire = next_quarter_first(now) if cadence == "quarterly" else next_month_first(now)
        auto = (c.get("delivery_mode") or "approval") == "auto"
        if paused:
            label = "Paused — scheduler is off"
        elif auto:
            label = f"{_day(fire)}, 09:00 UTC · auto-send"
        else:
            label = f"{_day(fire)} draft, then your approval"
        items.append({
            "kind": "scheduled", "draft_id": None, "invoice_id": None,
            "subscription_id": sid, "customer_name": c.get("customer_name"),
            "email": c.get("client_email"), "send_mode": c.get("send_mode"),
            "delivery_mode": c.get("delivery_mode"), "cadence": cadence,
            "period_label": None,
            "amount_usd": c.get("last_sent_amount_usd"),
            "amount_is_estimate": True,
            "kwh": None, "invoice_number": None,
            "utility": _utility_view(c), "arrays": c.get("arrays") or [],
            "when": _iso(fire), "when_label": label,
            "status": "paused" if paused else ("auto" if auto else "approval"),
            "reason": None, "created_at": None, "sort": 2,
        })

    items.sort(key=lambda x: (x["sort"], x.get("when") or "", (x.get("customer_name") or "").lower()))
    return items


# ── one invoice, everything ────────────────────────────────────────────────

def invoice_detail(db, tenant_id: str, invoice_id: int) -> Optional[dict]:
    from ..models import (OfftakerInvoice, BillingEmailDispatch, OfftakerPayment,
                          OfftakerSettlement)
    inv = db.get(OfftakerInvoice, invoice_id)
    if inv is None or inv.tenant_id != tenant_id:
        return None
    ctx = sub_context(db, tenant_id)
    dispatch = db.execute(
        select(BillingEmailDispatch).where(BillingEmailDispatch.tenant_id == tenant_id,
                                           BillingEmailDispatch.key == f"invoice:{inv.id}")
    ).scalars().first()
    payment = db.scalar(select(OfftakerPayment).where(OfftakerPayment.id == inv.payment_id,
        OfftakerPayment.tenant_id == tenant_id)) if inv.payment_id else None
    settlements = db.execute(
        select(OfftakerSettlement).where(OfftakerSettlement.tenant_id == tenant_id,
                                         OfftakerSettlement.invoice_id == inv.id)
        .order_by(OfftakerSettlement.received_on.desc())
    ).scalars().all()
    events = _delivery_events(db, {dispatch.resend_email_id} if dispatch is not None and dispatch.resend_email_id else set())
    item = _sent_item(inv, dispatch, payment, sum(int(s.amount_cents) for s in settlements),
                      events, ctx.get(inv.subscription_id) or {})
    snap = inv.snapshot or {}
    ci = snap.get("computed_invoice") or {}
    rs = inv.render_snapshot or {}
    env = dict(dispatch.email) if dispatch is not None and isinstance(dispatch.email, dict) else (
        (rs.get("variants") or {}).get("online") or (rs.get("variants") or {}).get("offline") or {})
    figures = {k: v for k, v in ci.items() if k not in ("bill_anatomy",) and not isinstance(v, (bytes, bytearray))}
    item.update({
        "figures": figures,
        "bill_anatomy": ci.get("bill_anatomy"),
        "project_totals": snap.get("project_totals"),
        "email": {"subject": env.get("subject"), "html": env.get("html"),
                  "text": env.get("text"), "from_addr": env.get("from_addr"),
                  "reply_to": env.get("reply_to")},
        "attachment_files": [{"filename": a.get("filename"),
                              "size_bytes": (len(a.get("content") or "") * 3) // 4,
                              "sha256": (rs.get("artifact_sha256") or {}).get(a.get("filename"))}
                             for a in (rs.get("attachments") or []) if a.get("filename")],
        "settlements": [{"id": s.id, "amount_usd": _money(s.amount_cents),
                         "received_on": _iso(s.received_on), "method": s.method,
                         "actor": s.actor, "note": s.note, "created_at": _iso(s.created_at)}
                        for s in settlements],
        "prepared_at": rs.get("prepared_at"),
        "last_error": inv.last_error,
        "subscription": {k: v for k, v in (ctx.get(inv.subscription_id) or {}).items()
                         if not k.startswith("_")},
    })
    return item


def attachment_bytes(db, tenant_id: str, invoice_id: int, filename: str) -> Optional[tuple[bytes, str]]:
    """The exact attachment the off-taker received (frozen at issue time)."""
    import base64
    from ..models import OfftakerInvoice
    inv = db.get(OfftakerInvoice, invoice_id)
    if inv is None or inv.tenant_id != tenant_id:
        return None
    for a in ((inv.render_snapshot or {}).get("attachments") or []):
        if a.get("filename") == filename and a.get("content"):
            ctype = a.get("content_type") or a.get("type") or (
                "application/pdf" if filename.lower().endswith(".pdf") else
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if filename.lower().endswith(".xlsx") else "application/octet-stream")
            try:
                return base64.b64decode(a["content"]), ctype
            except Exception:  # noqa: BLE001
                return None
    return None


# ── the whole board ────────────────────────────────────────────────────────

def portfolio_counts(db, tenant_id):
    """Global KPIs from narrow columns; never load archived HTML/PDF payloads."""
    from sqlalchemy import func, String, cast
    from ..models import OfftakerInvoice as I, OfftakerPayment as P, OfftakerSettlement as S, BillingEmailDispatch as D
    offline = select(S.invoice_id, func.sum(S.amount_cents).label("paid")).where(
        S.tenant_id == tenant_id).group_by(S.invoice_id).subquery()
    rows = db.execute(select(I.status, I.amount_cents, P.id, P.status, P.amount_cents,
        P.refunded_cents, offline.c.paid, D.status, D.resend_email_id)
        .outerjoin(P, (P.id == I.payment_id) & (P.tenant_id == tenant_id))
        .outerjoin(offline, offline.c.invoice_id == I.id)
        .outerjoin(D, (D.tenant_id == tenant_id) & (D.key == "invoice:" + cast(I.id, String)))
        .where(I.tenant_id == tenant_id, I.status.in_(SENT_STATUSES)))
    out = {"paid": 0, "unpaid": 0, "unconfirmed": 0, "bounced": 0,
           "billed_usd": 0.0, "collected_usd": 0.0}
    receipts = set()
    billed = collected = 0
    for status, amount, payment_id, ps, pa, refund, off, dispatch_status, receipt in rows:
        if receipt:
            receipts.add(receipt)
        if status == "uncertain" or dispatch_status in ("sending", "uncertain"):
            out["unconfirmed"] += 1
        gross = (int(pa or 0) if ps in ("paid", "refunded") else 0) + int(off or 0)
        refunded = int(refund or 0) if ps in ("paid", "refunded") else 0
        if status == "accepted":
            billed += int(amount or 0)
            collected += max(0, gross - refunded)
        # Match _sent_item payment_summary exactly, including zero-dollar
        # obligations with no payment identity and fully refunded payments.
        if payment_id is None and not off:
            continue  # not_tracked is neither paid nor unpaid
        if refunded and refunded >= gross:
            continue  # refunded
        if amount is not None and gross >= amount:
            out["paid"] += 1
        else:
            out["unpaid"] += 1  # unpaid or partial
    ids = list(receipts)
    for offset in range(0, len(ids), 500):
        events = _delivery_events(db, set(ids[offset:offset + 500]))
        out["bounced"] += sum(v.get("status") == "bounced" for v in events.values())
    out["billed_usd"] = round(billed / 100, 2)
    out["collected_usd"] = round(collected / 100, 2)
    return out


def board(db, tenant_id: str, tenant, *, limit: int = 300, offset: int = 0,
          include_legacy: bool = True) -> dict:
    ctx = sub_context(db, tenant_id)
    sent, total = sent_items(db, tenant_id, limit=limit, offset=offset, ctx=ctx)
    all_legacy = legacy_items(db, tenant_id, ctx, known_frozen_periods(db, tenant_id)) if include_legacy else []
    legacy = all_legacy if offset == 0 else []
    outgoing = outgoing_items(db, tenant_id, tenant, ctx=ctx)
    sent_all = sent + legacy
    counts = {
        "outgoing": len(outgoing),
        "drafts": sum(1 for x in outgoing if x["kind"] == "draft"),
        "held": sum(1 for x in outgoing if x["kind"] in ("held", "retrying", "unconfirmed", "prepared")),
        "scheduled": sum(1 for x in outgoing if x["kind"] == "scheduled"),
        "sent_total": int(total) + len(all_legacy),
        "sent_frozen": int(total),
        "sent_legacy": len(all_legacy),
        "bounced": sum(1 for x in sent_all if (x.get("delivery") or {}).get("status") == "bounced"),
        "unconfirmed": sum(1 for x in sent_all if (x.get("delivery") or {}).get("status") == DELIVERY_UNCONFIRMED),
        "paid": sum(1 for x in sent_all if x.get("payment_summary") == "paid"),
        "unpaid": sum(1 for x in sent_all if x.get("payment_summary") in ("unpaid", "partial")),
        "billed_usd": round(sum(float(x.get("amount_usd") or 0) for x in sent_all), 2),
        "collected_usd": round(sum(float(x.get("paid_usd") or 0) for x in sent_all), 2),
    }
    return {
        "ok": True,
        "generated_at": _iso(datetime.utcnow()),
        "paused": bool(getattr(tenant, "sending_paused", False)),
        "outgoing": outgoing,
        "sent": sent_all,
        "counts": {**counts, **portfolio_counts(db, tenant_id)},
        "page_counts": {**counts, "sent_total": len(sent_all), "sent_frozen": len(sent), "sent_legacy": len(legacy)},
        "counts_scope": "portfolio_frozen_invoices",
        "legacy_scope": "first_page_only",
        "has_more": offset + len(sent) < total,
        "limit": limit, "offset": offset,
    }


# ── one legacy send, what we still have ────────────────────────────────────

def legacy_detail(db, tenant_id: str, kind: str, ref_id: int) -> Optional[dict]:
    """A send from before the frozen table: the approval-inbox row or the
    subscription's last-send stamp, plus the archived email if the mailer
    receipt still matches one. Honest about what is missing."""
    from ..models import ReportDraft, BillingReportSubscription
    ctx = sub_context(db, tenant_id)
    if kind == "draft":
        d = db.get(ReportDraft, ref_id)
        if d is None or d.tenant_id != tenant_id:
            return None
        c = ctx.get(d.subscription_id) or {}
        item = _legacy_item(f"legacy:draft:{d.id}", d.subscription_id, c,
                            invoice_number=d.invoice_number, amount=d.amount_usd,
                            kwh=d.customer_kwh, period_label=d.period_label,
                            sent_at=d.sent_at or d.created_at)
        item["array_kwh"] = d.array_total_kwh
        item["allocation_pct"] = d.allocation_pct if d.allocation_pct is not None else item.get("allocation_pct")
        item["figures"] = {"amount_owed": d.amount_usd, "kwh": d.customer_kwh,
                           "project_total_kwh": d.array_total_kwh, "allocation_pct": d.allocation_pct,
                           "invoice_number": d.invoice_number, "period_label": d.period_label,
                           "note": d.note, "gmp_attachment": d.gmp_filename}
        sent_at = d.sent_at or d.created_at
        resend_id = c.get("last_resend_email_id") if (
            c.get("last_sent_at") and sent_at and abs((datetime.fromisoformat(c["last_sent_at"].rstrip("Z")) - sent_at).total_seconds()) < 3600) else None
    elif kind == "sub":
        sub = db.get(BillingReportSubscription, ref_id)
        if sub is None or sub.tenant_id != tenant_id:
            return None
        c = ctx.get(sub.id) or {}
        item = _legacy_item(f"legacy:sub:{sub.id}", sub.id, c,
                            invoice_number=c.get("last_invoice_number"),
                            amount=c.get("last_sent_amount_usd"), kwh=None,
                            period_label=c.get("last_sent_period_end"), sent_at=c.get("last_sent_at"))
        item["figures"] = {"amount_owed": c.get("last_sent_amount_usd"),
                           "invoice_number": c.get("last_invoice_number"),
                           "period_end": c.get("last_sent_period_end")}
        resend_id = c.get("last_resend_email_id")
        sent_at = None
    else:
        return None
    email = {"subject": None, "html": None, "text": None, "from_addr": None, "reply_to": None}
    try:
        from ..email_archive import EmailArchive
        row = None
        if resend_id:
            row = db.execute(select(EmailArchive).where(EmailArchive.resend_id == resend_id)).scalars().first()
        if row is None and item.get("to") and sent_at is not None:
            lo, hi = sent_at - timedelta(hours=2), sent_at + timedelta(hours=2)
            row = db.execute(
                select(EmailArchive).where(EmailArchive.to_email == item["to"][0],
                                           EmailArchive.created_at >= lo,
                                           EmailArchive.created_at <= hi)
                .order_by(EmailArchive.created_at.desc())).scalars().first()
        if row is not None:
            email = {"subject": row.subject, "html": row.body_html, "text": row.body_text,
                     "from_addr": row.from_addr, "reply_to": None}
            if row.cc:
                item["cc"] = _as_list(row.cc)
            if row.bcc:
                item["bcc"] = _as_list(row.bcc)
            item["from_addr"] = row.from_addr
            item["subject"] = row.subject
            if row.attachments:
                item["attachments"] = [a.strip().split("(")[0].strip() for a in row.attachments.split(",") if a.strip()]
            if row.dry_run:
                item["delivery"] = {"status": "dry_run", "delivered_at": None, "bounced_at": None,
                                    "reason": "sent in dry-run mode — nothing left the server"}
            elif not row.ok:
                item["delivery"] = {"status": "bounced" if "bounce" in (row.flags or "") else "unconfirmed",
                                    "delivered_at": None, "bounced_at": None, "reason": row.flags}
    except Exception:  # noqa: BLE001
        logger.warning("mailroom: legacy email lookup failed", exc_info=True)
    item.update({"email": email, "attachment_files": [], "settlements": [],
                 "prepared_at": None, "last_error": None, "evidence": "legacy",
                 "subscription": {k: v for k, v in (ctx.get(item["subscription_id"]) or {}).items()
                                  if not k.startswith("_")}})
    return item

