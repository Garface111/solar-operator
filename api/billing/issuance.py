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
    d.pop("_source_evidence", None)  # archive metadata is not a BillingMatch field
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
        from .source_evidence import archive
        frozen_snapshot = match.to_dict()
        frozen_snapshot["_source_evidence"] = archive(db, sub, match)
        values = dict(tenant_id=tenant_id, subscription_id=subscription_id,
            period_key=key, period_start=start, period_end=end,
            invoice_number=str(ci.get("invoice_number") or ""), amount_cents=amount,
            credit_applied_cents=credit, customer_kwh=ci.get("kwh"),
            snapshot=frozen_snapshot, render_snapshot=rendered, status="prepared")
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
        _finish_locked(db, row, result, payment_id=payment_id)
        db.commit()



def _finish_locked(db, row, result, *, payment_id=None):
    row.payment_id = payment_id or row.payment_id
    if row.status == "accepted" and row.applied_at:
        return
    row.status = "accepted" if result.get("ok") else ("uncertain" if result.get("uncertain") else "failed")
    row.last_error = result.get("error")
    if result.get("ok"):
        sub = db.scalar(select(BillingReportSubscription).where(
            BillingReportSubscription.id == row.subscription_id).with_for_update())
        stamp = result.get("accepted_at") or datetime.utcnow()
        row.sent_at = stamp
        ci = row.snapshot.get("computed_invoice") or {}
        if not row.applied_at:
            # Recovery may complete an older accepted dispatch after a newer
            # invoice. Never roll the subscription's latest-send state backward.
            latest_period = str(sub.last_sent_period_end or "")[:10]
            keep_latest = (
                (sub.last_sent_at is not None and sub.last_sent_at > stamp)
                or (latest_period and row.period_end.isoformat() < latest_period)
            )
            if not keep_latest:
                sub.last_sent_at = stamp
                sub.last_invoice_number = row.invoice_number
                sub.last_sent_amount_usd = row.amount_cents / 100
                sub.last_sent_customer_kwh = row.customer_kwh
            if ci.get("is_trueup"):
                if not keep_latest and (sub.last_trueup_window_end is None
                                        or sub.last_trueup_window_end < row.period_end):
                    sub.last_trueup_window_end = row.period_end
                credit = ci.get("trueup_credit_usd", ci.get("credit_usd", 0))
                sub.pending_credit_usd = (cents(sub.pending_credit_usd) + cents(credit)) / 100
            elif not keep_latest:
                sub.last_sent_period_end = row.period_end.isoformat()
                from .delivery import next_send_at
                sub.next_send_at = next_send_at(sub.cadence, stamp)
            if not keep_latest and result.get("resend_email_id"):
                sub.last_resend_email_id = result["resend_email_id"]
            override_id = (row.render_snapshot or {}).get("email_copy_override_id")
            if override_id:
                from ..models import EmailCopyOverride
                from ..email_copy_overrides import record_send
                override = db.scalar(select(EmailCopyOverride).where(
                    EmailCopyOverride.id == override_id,
                    EmailCopyOverride.tenant_id == row.tenant_id).with_for_update())
                if override is None:
                    raise ValueError("Frozen email override is unavailable for this tenant")
                record_send(db, override.id)
            row.applied_at = stamp

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


def repair_accepted_invoice(*, tenant_id, invoice_id, audit_run_id):
    """Repair local acceptance bookkeeping only, atomically with its audit log."""
    from ..models import OfftakerAuditRun
    with SessionLocal() as db:
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        row = db.scalar(select(OfftakerInvoice).where(
            OfftakerInvoice.id == invoice_id, OfftakerInvoice.tenant_id == tenant_id
        ).with_for_update())
        if row is None:
            return None
        dispatch = db.scalar(select(BillingEmailDispatch).where(
            BillingEmailDispatch.tenant_id == tenant_id,
            BillingEmailDispatch.key == f"invoice:{invoice_id}").with_for_update())
        if dispatch is None or dispatch.status != "accepted":
            return None
        if row.status == "accepted" and row.applied_at:
            return None
        before = {"status": row.status, "applied_at": row.applied_at.isoformat() if row.applied_at else None}
        ci = (row.snapshot or {}).get("computed_invoice") or {}
        sub = db.scalar(select(BillingReportSubscription).where(
            BillingReportSubscription.id == row.subscription_id,
            BillingReportSubscription.tenant_id == tenant_id).with_for_update())
        reason = None
        if sub is None:
            reason = "Subscription ownership is unavailable; review manually."
        elif ci.get("is_trueup") or str(row.period_key).startswith("trueup:"):
            reason = "True-up acceptance can adjust credits; review manually."
        elif not row.snapshot or row.amount_cents is None or row.period_end is None:
            reason = "Frozen invoice evidence is incomplete; review manually."
        if reason is None:
            _finish_locked(db, row, {"ok": True, "resend_email_id": dispatch.resend_email_id,
                "accepted_at": row.sent_at or dispatch.updated_at or dispatch.created_at})
        repair = {"code": "accepted_dispatch_recovered", "invoice_id": row.id,
            "dispatch_id": dispatch.id, "status": "blocked" if reason else "repaired",
            "reason": reason or "Recorded an already accepted send; no email was sent.",
            "before": before, "after": {"status": row.status,
                "applied_at": row.applied_at.isoformat() if row.applied_at else None}}
        run = db.scalar(select(OfftakerAuditRun).where(
            OfftakerAuditRun.id == audit_run_id, OfftakerAuditRun.tenant_id == tenant_id,
            OfftakerAuditRun.status == "running").with_for_update())
        if run is None:
            db.rollback()
            return None
        stats = dict(run.stats or {})
        stats["repairs"] = list(stats.get("repairs") or []) + [repair]
        stats["repaired_count"] = sum(r["status"] == "repaired" for r in stats["repairs"])
        run.stats = stats
        db.commit()
        return repair
