"""Durable email outbox. An uncertain external effect requires reconciliation.

No automatic lease expiry: a crashed worker may have transmitted its email.
A permanent DB key protects retries beyond the provider idempotency window.
"""
from datetime import datetime, timedelta
import hashlib
from sqlalchemy import select, update, or_, text
from sqlalchemy.exc import IntegrityError
from ..db import SessionLocal
from ..models import BillingEmailDispatch, BillingEmailRateGate


def get_dispatch_status(tenant_id, key):
    with SessionLocal() as db:
        row = db.scalar(select(BillingEmailDispatch).where(
            BillingEmailDispatch.tenant_id == tenant_id, BillingEmailDispatch.key == key))
        return row.status if row else None


def wait_for_send_slot():
    """Conservative one billing email/second by default, shared across processes.

    No invoice is claimed while waiting: process death before transmission is
    safely retryable. Locks and pooled connections are released before sleeping.
    """
    import os
    import time
    interval = max(.001, float(os.getenv("BILLING_EMAIL_INTERVAL_SECONDS", "1")))
    while True:
        with SessionLocal() as db:
            if db.bind.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            row = db.scalar(select(BillingEmailRateGate).where(
                BillingEmailRateGate.id == "resend-billing").with_for_update())
            if row is None:
                row = BillingEmailRateGate(id="resend-billing", next_at=datetime.utcnow())
                db.add(row)
                try:
                    db.flush()
                except IntegrityError:
                    db.rollback()
                    continue
            now = datetime.utcnow()
            delay = (row.next_at - now).total_seconds()
            if delay <= 0:
                row.next_at = now + timedelta(seconds=interval)
                db.commit()
                return
        time.sleep(min(delay, 1))


def send_email_once(*, tenant_id: str, key: str, email: dict, kind="invoice") -> dict:
    from .. import notify
    now = datetime.utcnow()
    with SessionLocal() as db:
        row = db.scalar(select(BillingEmailDispatch).where(
            BillingEmailDispatch.tenant_id == tenant_id, BillingEmailDispatch.key == key))
        if row is None:
            row = BillingEmailDispatch(tenant_id=tenant_id, key=key, email=email, kind=kind)
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.scalar(select(BillingEmailDispatch).where(
                    BillingEmailDispatch.tenant_id == tenant_id, BillingEmailDispatch.key == key))
        row_id = row.id
        if (row.status not in ("prepared", "failed") or row.attempts >= 8
                or row.retry_at is not None and row.retry_at > now):
            return {"ok": row.status == "accepted", "duplicate": True,
                    "uncertain": row.status in ("sending", "uncertain"),
                    "resend_email_id": row.resend_email_id, "error": row.error or row.status,
                    "retry_at": row.retry_at.isoformat() if row.retry_at else None}
    wait_for_send_slot()
    now = datetime.utcnow()
    with SessionLocal() as db:
        row = db.get(BillingEmailDispatch, row_id)
        claimed = db.execute(update(BillingEmailDispatch).where(
            BillingEmailDispatch.id == row_id,
            BillingEmailDispatch.status.in_(["prepared", "failed"]),
            BillingEmailDispatch.attempts < 8,
            or_(BillingEmailDispatch.retry_at.is_(None), BillingEmailDispatch.retry_at <= now),
        ).values(status="sending", attempts=BillingEmailDispatch.attempts + 1,
                 updated_at=now)).rowcount
        db.commit()
        db.refresh(row)
        if not claimed:
            return {"ok": row.status == "accepted", "duplicate": True,
                    "uncertain": row.status in ("sending", "uncertain"),
                    "resend_email_id": row.resend_email_id, "error": row.error or row.status,
                    "retry_at": row.retry_at.isoformat() if row.retry_at else None}
        payload, attempts = dict(row.email), row.attempts
    payload["tags"] = [{"name": "billing_dispatch", "value": str(row_id)}]
    stable_key = "billing-" + hashlib.sha256(f"{tenant_id}:{key}".encode()).hexdigest()
    notify._send_outcome.set("not_sent")
    notify._send_failure.set({})
    try:
        ok = notify._send_via_resend(**payload, idempotency_key=stable_key)
        receipt = notify.last_resend_id() if ok else None
        uncertain = not ok and notify._send_outcome.get() != "not_sent"
        error = None if ok else str(notify._send_failure.get().get("error") or getattr(notify._send_via_resend, "_last_error", None) or "email rejected")
    except Exception as exc:
        ok, receipt, uncertain, error = False, None, True, str(exc)
    # This commit is separate from the invoice caller's transaction. If it fails,
    # persisted 'sending' deliberately prevents a dangerous automatic retransmit.
    with SessionLocal() as db:
        row = db.get(BillingEmailDispatch, row_id)
        row.status = "accepted" if ok else ("uncertain" if uncertain else "failed")
        row.resend_email_id, row.error = receipt, error
        row.retry_at = None if ok or uncertain else datetime.utcnow() + timedelta(seconds=max(float(notify._send_failure.get().get("retry_after") or 0), min(86400, 60 * 2 ** (attempts - 1))))
        db.commit()
    return {"ok": bool(ok), "duplicate": False, "uncertain": uncertain,
            "resend_email_id": receipt, "error": error}


def retry_due_dispatches(limit=100):
    """Resume only proven-unsent outbox entries; unknown external effects stay held."""
    from ..models import Tenant, OfftakerInvoice, BillingReportSubscription
    now = datetime.utcnow()
    with SessionLocal() as db:
        rows = db.scalars(select(BillingEmailDispatch).where(
            BillingEmailDispatch.status.in_(["prepared", "failed"]),
            BillingEmailDispatch.attempts < 8,
            or_(BillingEmailDispatch.retry_at.is_(None), BillingEmailDispatch.retry_at <= now))
            .order_by(BillingEmailDispatch.created_at).execution_options(yield_per=100))
        pending = []
        for row in rows:
            tenant = db.get(Tenant, row.tenant_id)
            if not tenant or tenant.sending_paused or not (tenant.active or tenant.subscription_status in ("comped", "trialing")):
                continue
            if row.key.startswith("invoice:"):
                invoice = db.get(OfftakerInvoice, int(row.key.split(":")[1]))
                sub = db.get(BillingReportSubscription, invoice.subscription_id) if invoice else None
                if not sub or sub.deleted_at or not sub.enabled:
                    continue
            pending.append((row.tenant_id, row.key, row.email, row.kind))
            if len(pending) >= limit:
                break
    outcomes = []
    for tenant_id, key, email, kind in pending:
        try:
            result = send_email_once(tenant_id=tenant_id, key=key, email=email, kind=kind)
            if key.startswith("invoice:"):
                from .issuance import finish
                finish(int(key.split(":")[1]), result)
            outcomes.append({"key": key, **result})
        except Exception as exc:
            outcomes.append({"key": key, "ok": False, "error": str(exc)})
    return outcomes


def reconcile_provider_receipt(*, tenant_id, dispatch_id, receipt_id, actor):
    """An operator supplies a provider ID; authenticated provider evidence decides.

    This endpoint never sends an email or clears an uncertain claim for retry.
    """
    from .. import notify
    import resend
    with SessionLocal() as db:
        row = db.scalar(select(BillingEmailDispatch).where(
            BillingEmailDispatch.id == dispatch_id, BillingEmailDispatch.tenant_id == tenant_id))
        if row is None:
            raise LookupError("Dispatch not found")
        expected = dict(row.email)
        key = row.key
    resend.api_key = notify.RESEND_API_KEY
    observed = resend.Emails.get(email_id=receipt_id)
    tags = {v.get("name"):v.get("value") for v in observed.get("tags", [])}
    if observed.get("id") != receipt_id or tags.get("billing_dispatch") != str(dispatch_id):
        raise ValueError("Provider receipt does not identify this dispatch")
    def addresses(value):
        if not value: return []
        return sorted([value] if isinstance(value, str) else value)
    if any(addresses(observed.get(k)) != addresses(expected.get(k)) for k in ("to", "cc", "bcc")):
        raise ValueError("Provider receipt recipients differ from the frozen invoice")
    if any((observed.get(k) or "") != (expected.get(k) or "") for k in ("subject", "html", "text")):
        raise ValueError("Provider receipt content differs from the frozen invoice")
    if observed.get("last_event") not in ("sent", "delivered", "delivery_delayed", "bounced", "complained", "opened", "clicked", "suppressed"):
        raise ValueError("Provider has not accepted this email for delivery")
    with SessionLocal() as db:
        row = db.scalar(select(BillingEmailDispatch).where(
            BillingEmailDispatch.id == dispatch_id, BillingEmailDispatch.tenant_id == tenant_id).with_for_update())
        if row.resend_email_id and row.resend_email_id != receipt_id:
            raise ValueError("Dispatch already references a different provider receipt")
        row.status = "accepted"
        row.resend_email_id = receipt_id
        row.error = f"Provider evidence reconciled by {actor}"
        row.retry_at = None
        db.commit()
    result = {"ok": True, "resend_email_id": receipt_id, "reconciled": True}
    if key.startswith("invoice:"):
        from .issuance import finish
        finish(int(key.split(":")[1]), result)
    return result
