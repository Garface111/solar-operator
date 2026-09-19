"""Durable discovery of every closed billing period since subscription onboarding.

Creation is the earliest automatic boundary absent older issued evidence. Importing
historical utility data never authorizes invoices from before onboarding.
"""
from calendar import monthrange
from datetime import date, datetime
import re
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ..models import OfftakerInvoice, OfftakerPayment, ReportDraft


def canonical_period(value, cadence="monthly"):
    value = str(value or "")
    if re.fullmatch(r"\d{4}-Q[1-4]", value):
        return value
    match = re.search(r"(\d{4})-(\d{2})(?:-\d{2})?$", value)
    if not match:
        return None
    year, month = map(int, match.groups())
    if not 1 <= month <= 12:
        return None
    return f"{year:04d}-Q{(month-1)//3+1}" if cadence == "quarterly" else f"{year:04d}-{month:02d}"


def bounds(key):
    year = int(key[:4])
    if "Q" in key:
        start_month = (int(key[-1])-1)*3+1
        end_month = start_month+2
    else:
        start_month = end_month = int(key[5:7])
    return date(year,start_month,1), date(year,end_month,monthrange(year,end_month)[1])


def queue_closed_periods(db, sub, *, today=None):
    """Persist missing obligations and return retryable period keys oldest first."""
    today = today or date.today()
    cadence = sub.cadence or "monthly"
    created = sub.created_at.date() if isinstance(sub.created_at, datetime) else sub.created_at
    if created is None:
        return []  # Unknown onboarding date requires operator reconciliation.
    first = canonical_period(created.isoformat(), cadence)
    invoices = db.scalars(select(OfftakerInvoice).where(
        OfftakerInvoice.tenant_id == sub.tenant_id,
        OfftakerInvoice.subscription_id == sub.id)).all()
    from .issuance import reconcile
    blocked_ids = set()
    for inv in invoices:
        if inv.status != "accepted":
            result = reconcile(inv.id)
            if result.get("ok") or result.get("uncertain"):
                blocked_ids.add(inv.id)
                db.refresh(inv)
    by_key = {i.period_key:i for i in invoices if not i.period_key.startswith("trueup:")}
    legacy_payments = {canonical_period(p.period_key,cadence) for p in db.scalars(
        select(OfftakerPayment).where(OfftakerPayment.tenant_id == sub.tenant_id,
        OfftakerPayment.subscription_id == sub.id))}
    legacy_payments.discard(None)
    # Payment-link creation precedes sending and is never proof of delivery.
    # Existing immutable obligations control retries even if a link already exists.
    legacy_payments.difference_update(by_key)
    legacy = {canonical_period(sub.last_sent_period_end,cadence)} - {None}
    evidence = list(by_key) + list(legacy) + list(legacy_payments)
    if evidence:
        earliest = min([bounds(first)[0]]+[bounds(k)[0] for k in evidence])
        first = canonical_period(earliest.isoformat(),cadence)
    pending = {canonical_period(d.period_label,cadence) for d in db.scalars(select(ReportDraft).where(
        ReportDraft.tenant_id == sub.tenant_id, ReportDraft.subscription_id == sub.id,
        ReportDraft.status == "pending"))}
    current = bounds(first)[0]
    retry = []
    while current < today:
        key = canonical_period(current.isoformat(),cadence)
        start,end = bounds(key)
        if end >= today:
            break
        inv = by_key.get(key)
        # Cadence overlaps are logical month/quarter identities. Meter-read
        # spans can cross month boundaries without billing the same cycle twice.
        overlaps = [i for i in invoices if i.period_key != key
                    and not i.period_key.startswith("trueup:")
                    and bounds(i.period_key)[0] <= end and bounds(i.period_key)[1] >= start]
        if overlaps:
            for old in overlaps:
                if old.status != "accepted":
                    old.last_error = "Cadence changed across an existing obligation; reconcile before retry"
            next_month = end.month+1
            current = date(end.year+1,1,1) if next_month == 13 else date(end.year,next_month,1)
            continue
        if key not in legacy and inv is None:
            inv = OfftakerInvoice(tenant_id=sub.tenant_id,subscription_id=sub.id,
                period_key=key,period_start=start,period_end=end,status="held",
                snapshot={},last_error=("Legacy payment exists without delivery evidence; reconcile before sending" if key in legacy_payments else "Awaiting source evidence and scheduled billing review"))
            try:
                with db.begin_nested():
                    db.add(inv); db.flush()
            except IntegrityError:
                inv = db.scalar(select(OfftakerInvoice).where(
                    OfftakerInvoice.tenant_id == sub.tenant_id,
                    OfftakerInvoice.subscription_id == sub.id,OfftakerInvoice.period_key == key))
        if key not in legacy and key not in legacy_payments and key not in pending and inv is not None and inv.id not in blocked_ids and inv.status not in ("accepted","sending","uncertain"):
            retry.append(key)
        next_month = end.month+1
        current = date(end.year+1,1,1) if next_month == 13 else date(end.year,next_month,1)
    db.commit()
    return retry


def record_hold(db, sub, key, reason):
    row = db.scalar(select(OfftakerInvoice).where(OfftakerInvoice.tenant_id == sub.tenant_id,
        OfftakerInvoice.subscription_id == sub.id,OfftakerInvoice.period_key == key))
    if row and row.status not in ("accepted","sending","uncertain"):
        row.last_error = str(reason)[:1000]
        db.commit()
