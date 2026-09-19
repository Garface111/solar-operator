"""Durable email outbox. An uncertain external effect requires reconciliation.

No automatic lease expiry: a crashed worker may have transmitted its email.
A permanent DB key protects retries beyond the provider idempotency window.
"""
from datetime import datetime, timedelta
import hashlib
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from ..db import SessionLocal
from ..models import BillingEmailDispatch


def get_dispatch_status(tenant_id, key):
    with SessionLocal() as db:
        row = db.scalar(select(BillingEmailDispatch).where(
            BillingEmailDispatch.tenant_id == tenant_id, BillingEmailDispatch.key == key))
        return row.status if row else None


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
    stable_key = "billing-" + hashlib.sha256(f"{tenant_id}:{key}".encode()).hexdigest()
    notify._send_outcome.set("not_sent")
    try:
        ok = notify._send_via_resend(**payload, idempotency_key=stable_key)
        receipt = notify.last_resend_id() if ok else None
        uncertain = not ok and notify._send_outcome.get() != "not_sent"
        error = None if ok else str(getattr(notify._send_via_resend, "_last_error", None) or "email rejected")
    except Exception as exc:
        ok, receipt, uncertain, error = False, None, True, str(exc)
    # This commit is separate from the invoice caller's transaction. If it fails,
    # persisted 'sending' deliberately prevents a dangerous automatic retransmit.
    with SessionLocal() as db:
        row = db.get(BillingEmailDispatch, row_id)
        row.status = "accepted" if ok else ("uncertain" if uncertain else "failed")
        row.resend_email_id, row.error = receipt, error
        row.retry_at = None if ok or uncertain else datetime.utcnow() + timedelta(seconds=min(86400, 60 * 2 ** (attempts - 1)))
        db.commit()
    return {"ok": bool(ok), "duplicate": False, "uncertain": uncertain,
            "resend_email_id": receipt, "error": error}
