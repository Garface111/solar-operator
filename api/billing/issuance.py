"""Freeze each obligation and apply acceptance exactly once in independent transactions."""
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select, text
from ..db import SessionLocal
from ..models import OfftakerInvoice, BillingReportSubscription, BillingEmailDispatch
from .matcher import BillingMatch, Period


def cents(value):
    value = Decimal(str(value or 0))
    if not value.is_finite() or value < 0:
        raise ValueError("Invoice amounts must be finite and nonnegative")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def restore(snapshot):
    d = dict(snapshot)
    def period(value):
        if not value:
            return None
        value = dict(value)
        for key in ("start", "end"):
            if value.get(key):
                value[key] = date.fromisoformat(str(value[key])[:10])
        return Period(**value)
    d["periods"] = [period(v) for v in d.get("periods", [])]
    d["latest_period"] = period(d.get("latest_period"))
    return BillingMatch(**d)


def freeze(*, tenant_id, subscription_id, key, match, expected_amount=None, prepare=None):
    with SessionLocal() as db:
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        sub = db.scalar(select(BillingReportSubscription).where(
            BillingReportSubscription.id == subscription_id,
            BillingReportSubscription.tenant_id == tenant_id).with_for_update())
        if not sub:
            raise ValueError("subscription unavailable")
        row = db.scalar(select(OfftakerInvoice).where(
            OfftakerInvoice.tenant_id == tenant_id,
            OfftakerInvoice.subscription_id == subscription_id,
            OfftakerInvoice.period_key == key))
        if row and row.snapshot:
            if prepare is not None and not row.render_snapshot:
                raise ValueError("Frozen invoice lacks original rendered evidence; reconcile before retrying")
            return row.id, restore(row.snapshot), row.status
        ci = match.computed_invoice
        start = date.fromisoformat(str(ci.get("period_start"))[:10])
        end = date.fromisoformat(str(ci.get("period_end"))[:10])
        if start > end or end >= date.today():
            raise ValueError("Invoice requires a closed, dated billing period")
        # A cadence change must not rebill months covered by an existing invoice.
        if not ci.get("is_trueup"):
            from .backlog import bounds
            def months(period):
                a, b = bounds(period)
                return {(a.year, month) for month in range(a.month, b.month + 1)}
            coverage = months(key)
            existing = db.scalars(select(OfftakerInvoice).where(
                OfftakerInvoice.subscription_id == subscription_id,
                ~OfftakerInvoice.period_key.like("trueup:%"))).all()
            if any(other.snapshot and other.id != getattr(row, "id", None)
                   and coverage.intersection(months(other.period_key)) for other in existing):
                raise ValueError("Billing period overlaps an existing obligation")
            before = cents(ci.get("amount_before_credit", ci.get("amount_owed")))
            applied = min(before, cents(sub.pending_credit_usd))
            ci["credit_applied"] = applied / 100
            ci["amount_owed"] = (before - applied) / 100
            ci["pending_credit_remaining"] = (cents(sub.pending_credit_usd) - applied) / 100
        amount = cents(ci.get("amount_owed"))
        if expected_amount is not None and amount != cents(expected_amount):
            raise ValueError("Invoice amount changed since approval")
        if sub.invoice_number_next is not None:
            ci["invoice_number"] = str(sub.invoice_number_next)
            sub.invoice_number_next += 1
        # Reserve credits along with the invoice, so concurrent periods cannot
        # spend the same credit. A held invoice retains its visible reservation.
        credit = cents(ci.get("credit_applied"))
        sub.pending_credit_usd = (cents(sub.pending_credit_usd) - credit) / 100
        rendered = prepare(match) if prepare is not None else None
        values = dict(tenant_id=tenant_id, subscription_id=subscription_id,
            period_key=key, period_start=start, period_end=end,
            invoice_number=str(ci.get("invoice_number") or ""), amount_cents=amount,
            credit_applied_cents=credit, customer_kwh=ci.get("kwh"),
            snapshot=match.to_dict(), render_snapshot=rendered, status="prepared")
        if row is None:
            row = OfftakerInvoice(**values)
            db.add(row)
        else:
            for name, value in values.items():
                setattr(row, name, value)
        db.commit()
        return row.id, restore(row.snapshot), row.status


def finish(invoice_id, result, *, payment_id=None):
    with SessionLocal() as db:
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        row = db.scalar(select(OfftakerInvoice).where(OfftakerInvoice.id == invoice_id).with_for_update())
        row.payment_id = payment_id or row.payment_id
        if row.status == "accepted" and row.applied_at:
            return
        row.status = "accepted" if result.get("ok") else ("uncertain" if result.get("uncertain") else "failed")
        row.last_error = result.get("error")
        if result.get("ok"):
            sub = db.scalar(select(BillingReportSubscription).where(
                BillingReportSubscription.id == row.subscription_id).with_for_update())
            stamp = datetime.utcnow()
            row.sent_at = stamp
            ci = row.snapshot.get("computed_invoice") or {}
            if not row.applied_at:
                sub.last_sent_at = stamp
                sub.last_invoice_number = row.invoice_number
                sub.last_sent_amount_usd = row.amount_cents / 100
                sub.last_sent_customer_kwh = row.customer_kwh
                if ci.get("is_trueup"):
                    sub.last_trueup_window_end = row.period_end
                    credit = ci.get("trueup_credit_usd", ci.get("credit_usd", 0))
                    sub.pending_credit_usd = (cents(sub.pending_credit_usd) + cents(credit)) / 100
                else:
                    sub.last_sent_period_end = row.period_end.isoformat()
                    from .delivery import next_send_at
                    sub.next_send_at = next_send_at(sub.cadence, stamp)
                if result.get("resend_email_id"):
                    sub.last_resend_email_id = result["resend_email_id"]
                row.applied_at = stamp
        db.commit()


def reconcile(invoice_id):
    with SessionLocal() as db:
        row = db.get(OfftakerInvoice, invoice_id)
        dispatch = db.scalar(select(BillingEmailDispatch).where(
            BillingEmailDispatch.tenant_id == row.tenant_id,
            BillingEmailDispatch.key == f"invoice:{invoice_id}"))
        result = {"ok": dispatch is not None and dispatch.status == "accepted",
                  "uncertain": dispatch is not None and dispatch.status in ("sending", "uncertain"),
                  "resend_email_id": dispatch.resend_email_id if dispatch else None}
    if result["ok"]:
        finish(invoice_id, result)
    return result


def attach_payment(invoice_id, payment_id):
    """Record collection identity before any customer email can escape."""
    from ..models import OfftakerPayment
    with SessionLocal() as db:
        row = db.scalar(select(OfftakerInvoice).where(OfftakerInvoice.id == invoice_id).with_for_update())
        if payment_id is not None:
            payment = db.get(OfftakerPayment, payment_id)
            if (not payment or payment.tenant_id != row.tenant_id
                    or payment.subscription_id != row.subscription_id
                    or payment.amount_cents != row.amount_cents):
                raise ValueError("Invoice payment identity does not match frozen obligation")
            if row.payment_id and row.payment_id != payment_id:
                raise ValueError("Immutable invoice already has a different payment")
            row.payment_id = payment_id
        db.commit()


def hold(invoice_id, reason):
    with SessionLocal() as db:
        row = db.get(OfftakerInvoice, invoice_id)
        if row and row.status not in ("accepted", "sending", "uncertain"):
            row.status = "held"
            row.last_error = str(reason)[:1000]
            db.commit()


def load_frozen(tenant_id, subscription_id, key):
    """Read immutable evidence without touching current utility/workbook inputs."""
    if not key:
        return None
    with SessionLocal() as db:
        row = db.scalar(select(OfftakerInvoice).where(
            OfftakerInvoice.tenant_id == tenant_id,
            OfftakerInvoice.subscription_id == subscription_id,
            OfftakerInvoice.period_key == key))
        if row and row.snapshot:
            match = restore(row.snapshot)
            match._frozen_invoice_id = row.id
            return match
    return None


def rendered_evidence(invoice_id):
    with SessionLocal() as db:
        row = db.get(OfftakerInvoice, invoice_id)
        if not row or not row.render_snapshot:
            raise ValueError("Original rendered invoice evidence is unavailable")
        return dict(row.render_snapshot)
