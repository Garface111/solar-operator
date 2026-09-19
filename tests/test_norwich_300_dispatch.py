"""Offline 300-customer dispatch rehearsal; every external effect is replaced locally.

The companion scale test verifies real PDF/XLSX rendering. This test exercises
actual import, immutable issuance, outbox claims, retry and concurrent replay.
"""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Lock
import base64
import time
from sqlalchemy import select, update
from api.db import SessionLocal
from api.models import (Tenant, BillingReportSubscription, OfftakerInvoice,
                        BillingEmailDispatch, OfftakerPayment)
from api.billing import delivery, issuance, payments, routes
from api import notify
from tests.test_offtaker_upload import _make_tenant, _make_array_with_bill

B = "/v1/array-operator/billing"


def test_300_dispatch_reject_retry_concurrent_replay(client, monkeypatch):
    start = time.monotonic()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(routes, "_sync_invoicing_quantity", lambda *a: None)
    def forbidden_payment(*a, **kw):
        raise AssertionError("Explicit offline collection must not call Stripe")
    monkeypatch.setattr(payments, "create_offtaker_payment", forbidden_payment)
    monkeypatch.setattr(payments, "refresh_connect_status", forbidden_payment)
    monkeypatch.setattr(payments, "link_existing_connect_account", forbidden_payment)
    tid, auth = _make_tenant()
    headers = {"Authorization": auth}
    policy = client.patch(B + "/payment-policy", headers=headers, json={"policy": "offline"})
    assert policy.status_code == 200, policy.text
    arrays = [_make_array_with_bill(tid, f"Dispatch Array {i:03d}",
              f"DISPATCH-{i:03d}", with_bill=True) for i in range(100)]
    rows = [{"offtaker_name": f"Dispatch Offtaker {i:03d}",
             "array_id": arrays[i // 3][0], "utility_account_id": arrays[i // 3][1],
             "allocation_pct": .25, "net_rate_per_kwh": .18398,
             "discount_pct": .1, "email": f"dispatch{i:03d}@example.test"}
            for i in range(300)]
    roster = {"rows": rows, "delivery_mode": "approval", "cadence": "monthly"}
    response = client.post(B + "/subscriptions/bulk-commit", headers=headers, json=roster)
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 300 and not response.json()["failed"]
    repeated = client.post(B + "/subscriptions/bulk-commit", headers=headers, json=roster).json()
    assert repeated["created"] == 0 and len(repeated["skipped"]) == 300
    with SessionLocal() as db:
        subs = db.scalars(select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid).order_by(BillingReportSubscription.id)).all()
        ids = [s.id for s in subs]
        assert len(ids) == 300

    guard = Lock()
    attempts, accepted, rendered = Counter(), Counter(), Counter()
    provider_keys = defaultdict(set)
    rejected = {f"dispatch{i:03d}@example.test" for i in range(0, 300, 10)}
    def render(match, formats, include_summary, directory, **kwargs):
        name = match.customer["name"]
        assert match.computed_invoice["amount_owed"] == 206.98
        with guard:
            rendered[name] += 1
        path = directory / "invoice.pdf"
        path.write_bytes(f"Frozen test invoice: {name}; USD206.98".encode())
        return [path]
    monkeypatch.setattr(delivery, "generate_files", render)
    def transport(**kwargs):
        recipient = kwargs["to"]
        assert isinstance(recipient, str) and recipient.endswith("@example.test")
        assert "$206.98" in kwargs["html"] and "$206.98" in kwargs["text"]
        assert b"USD206.98" in base64.b64decode(kwargs["attachments"][0]["content"])
        key = kwargs["idempotency_key"]
        with guard:
            attempts[recipient] += 1
            provider_keys[recipient].add(key)
            if recipient in rejected and attempts[recipient] == 1:
                # Definitive pre-acceptance rejection; retry is safe.
                notify._send_outcome.set("not_sent")
                notify._send_failure.set({"error": "synthetic transient rejection"})
                notify._resend_receipt.set(None)
                return False
            accepted[recipient] += 1
            notify._send_outcome.set("accepted")
            notify._resend_receipt.set("synthetic-" + key[-32:])
            return True
    monkeypatch.setattr(notify, "_send_via_resend", transport)

    def send(sid, period=None, force=False):
        with SessionLocal() as db:
            return delivery.deliver_subscription(db, db.get(BillingReportSubscription, sid),
                db.get(Tenant, tid), period_label=period, force=force)
    initial = {sid: send(sid) for sid in ids}
    assert sum(bool(r.get("ok")) for r in initial.values()) == 270
    failed = [sid for sid, r in initial.items() if not r.get("ok")]
    assert len(failed) == 30 and sum(accepted.values()) == 270
    with SessionLocal() as db:
        invoices = db.scalars(select(OfftakerInvoice).where(OfftakerInvoice.tenant_id == tid)).all()
        assert len(invoices) == 300
        keys = {i.subscription_id: i.period_key for i in invoices}
        assert Counter(i.status for i in invoices) == {"accepted": 270, "failed": 30}
        assert all(i.render_snapshot and i.amount_cents == 20698 for i in invoices)
        # Credit arrives AFTER all original obligations froze. Retries/replays
        # must never consume this newly banked credit against the old invoice.
        db.execute(update(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid).values(pending_credit_usd=25))
        # Advance the retry eligibility without sleeping or changing the claims.
        db.execute(update(BillingEmailDispatch).where(BillingEmailDispatch.tenant_id == tid,
            BillingEmailDispatch.status == "failed").values(retry_at=datetime.utcnow()-timedelta(seconds=1)))
        db.commit()

    # Two independent callers compete for every retryable failed obligation.
    # The database/outbox owns arbitration, not a lock in the delivery caller.
    jobs = [sid for sid in failed for _ in range(2)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        retry_results = list(pool.map(lambda sid: send(sid, keys[sid], True), jobs))
    assert all(r.get("ok") or r.get("already_sent") or r.get("uncertain") for r in retry_results), retry_results
    # Reconcile every durable send, including callers that lost the race and
    # observed an in-flight dispatch while the winning thread completed.
    with SessionLocal() as db:
        invoice_ids = list(db.scalars(select(OfftakerInvoice.id).where(OfftakerInvoice.tenant_id == tid)))
    for invoice_id in invoice_ids:
        assert issuance.reconcile(invoice_id)["ok"]
    for sid in ids:
        replay = send(sid, keys[sid], True)
        assert replay.get("already_sent") and not replay.get("ok"), replay

    with SessionLocal() as db:
        invoices = db.scalars(select(OfftakerInvoice).where(OfftakerInvoice.tenant_id == tid)).all()
        dispatches = db.scalars(select(BillingEmailDispatch).where(BillingEmailDispatch.tenant_id == tid)).all()
        subs = db.scalars(select(BillingReportSubscription).where(BillingReportSubscription.tenant_id == tid)).all()
        assert len(invoices) == len(dispatches) == 300
        assert all(i.status == "accepted" and i.applied_at and i.sent_at for i in invoices)
        assert all(i.amount_cents == 20698 and i.credit_applied_cents == 0 for i in invoices)
        assert sum(i.amount_cents for i in invoices) == 6_209_400  # $62,094
        assert all(d.status == "accepted" for d in dispatches)
        assert sum(d.attempts for d in dispatches) == 330
        assert all(s.pending_credit_usd == 25 for s in subs)
        assert not db.scalars(select(OfftakerPayment).where(OfftakerPayment.tenant_id == tid)).all()
    assert len(accepted) == 300 and set(accepted.values()) == {1}
    assert sum(attempts.values()) == 330
    assert len({next(iter(v)) for v in provider_keys.values()}) == 300
    assert all(len(v) == 1 for v in provider_keys.values())
    assert len(rendered) == 300 and set(rendered.values()) == {1}
    print(f"300-offtaker import/dispatch/retry/concurrent replay: {time.monotonic()-start:.3f}s; "
          "300 accepted invoices, 330 attempts, 300 unique accepted emails, USD62094.00")
