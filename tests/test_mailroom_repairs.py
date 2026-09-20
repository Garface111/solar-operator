"""Mail Room repairs local bookkeeping; it cannot send or make financial decisions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest
from sqlalchemy import select

from api.db import SessionLocal
from api.models import (BillingReportSubscription, BillingEmailDispatch, OfftakerAuditRun,
                        OfftakerInvoice, Tenant, OfftakerPayment, OfftakerSettlement, EmailCopyOverride)
from api.billing import mailroom, mailroom_audit as audit, issuance
from tests.test_mailroom import _tenant as _make_test_tenant, _sub, _frozen, _dispatch, _auth, _B


_created_tenants = []


def _tenant(**kwargs):
    tenant = _make_test_tenant(**kwargs)
    _created_tenants.append(tenant.id)
    return tenant


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    from api import notify
    from api.billing import dispatch
    def forbidden(*a, **kw):
        pytest.fail("Mail Room check must not send messages or invoke a model")
    monkeypatch.setattr(notify, "_send_via_resend", forbidden)
    monkeypatch.setattr(dispatch, "send_email_once", forbidden)
    monkeypatch.setattr(audit, "model_review", forbidden)
    yield
    with SessionLocal() as db:
        for model in (OfftakerSettlement, OfftakerInvoice, OfftakerPayment,
                      BillingEmailDispatch, OfftakerAuditRun, BillingReportSubscription, EmailCopyOverride):
            db.query(model).filter(model.tenant_id.in_(_created_tenants)).delete(synchronize_session=False)
        db.query(Tenant).filter(Tenant.id.in_(_created_tenants)).delete(synchronize_session=False)
        db.commit()
    _created_tenants.clear()


def _run(tid):
    with SessionLocal() as db:
        run = OfftakerAuditRun(tenant_id=tid, status="running", stats={})
        db.add(run); db.commit()
        return run.id


def _setup(status="failed", dispatch_status="accepted"):
    t = _tenant()
    sid = _sub(t.id)
    with SessionLocal() as db:
        inv = _frozen(db, t.id, sid, "2026-06", status=status)
        dispatch = _dispatch(db, t.id, inv.id, status=dispatch_status)
        dispatch.updated_at = datetime(2026, 7, 1)
        db.commit()
        return t, sid, inv.id, dispatch.id


def test_check_repairs_accepted_send_once_without_financial_or_pause_changes():
    t, sid, iid, did = _setup()
    with SessionLocal() as db:
        tenant = db.get(Tenant, t.id)
        tenant.sending_paused = True
        sub = db.get(BillingReportSubscription, sid)
        sub.pending_credit_usd = 123
        db.commit()
    rid = _run(t.id)
    audit.execute_run(rid, tenant_id=t.id, rules_only=True)
    with SessionLocal() as db:
        inv = db.get(OfftakerInvoice, iid)
        assert inv.status == "accepted" and inv.applied_at
        assert inv.sent_at == datetime(2026, 7, 1)
        assert db.get(Tenant, t.id).sending_paused is True
        assert db.get(BillingReportSubscription, sid).pending_credit_usd == 123
        assert not db.scalars(select(OfftakerPayment).where(OfftakerPayment.tenant_id == t.id)).all()
        assert not db.scalars(select(OfftakerSettlement).where(OfftakerSettlement.tenant_id == t.id)).all()
        run = db.get(OfftakerAuditRun, rid)
        assert run.status == "done", run.error
        assert run.stats["repaired_count"] == 1
        assert run.stats["repairs"][0]["dispatch_id"] == did
        assert run.stats["coverage"]["checked"] == run.stats["coverage"]["total"] == 1
        assert run.stats["model_skipped"]
    next_run = _run(t.id)
    audit.execute_run(next_run, tenant_id=t.id, rules_only=True)
    with SessionLocal() as db:
        assert db.get(OfftakerAuditRun, next_run).stats["repaired_count"] == 0


def test_older_repair_preserves_newer_subscription_metadata():
    t, sid, iid, did = _setup()
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        sub.last_sent_at = datetime(2026, 9, 1)
        sub.last_sent_period_end = "2026-08-31"
        sub.last_invoice_number = "newest"
        sub.last_sent_amount_usd = 456
        sub.last_resend_email_id = "new-receipt"
        sub.next_send_at = datetime(2026, 10, 1)
        db.commit()
    issuance.repair_accepted_invoice(tenant_id=t.id, invoice_id=iid, audit_run_id=_run(t.id))
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        assert (sub.last_sent_at, sub.last_sent_period_end, sub.last_invoice_number,
                sub.last_sent_amount_usd, sub.last_resend_email_id, sub.next_send_at) == (
            datetime(2026, 9, 1), "2026-08-31", "newest", 456, "new-receipt", datetime(2026, 10, 1))


@pytest.mark.parametrize("status", ["sending", "uncertain", "failed", "prepared"])
def test_unproven_dispatches_never_repaired_or_retried(status):
    t, sid, iid, did = _setup(dispatch_status=status)
    audit.execute_run(_run(t.id), tenant_id=t.id, rules_only=True)
    with SessionLocal() as db:
        assert db.get(OfftakerInvoice, iid).status == "failed"
        assert db.get(BillingEmailDispatch, did).status == status


def test_tenant_isolation_and_trueup_credit_remains_manual():
    t, sid, iid, did = _setup()
    foreign = _tenant()
    assert issuance.repair_accepted_invoice(tenant_id=foreign.id, invoice_id=iid,
                                           audit_run_id=_run(foreign.id)) is None
    with SessionLocal() as db:
        inv = db.get(OfftakerInvoice, iid)
        inv.snapshot = {"computed_invoice": {"is_trueup": True, "credit_usd": 999}}
        db.commit()
    rid = _run(t.id)
    audit.execute_run(rid, tenant_id=t.id, rules_only=True)
    with SessionLocal() as db:
        inv = db.get(OfftakerInvoice, iid)
        assert inv.status == "failed" and inv.applied_at is None
        stats = db.get(OfftakerAuditRun, rid).stats
        assert stats["repaired_count"] == 0
        assert stats["repairs"][0]["status"] == "blocked"


def test_concurrent_repair_logs_effect_exactly_once():
    t, sid, iid, did = _setup()
    runs = [_run(t.id), _run(t.id)]
    barrier = Barrier(2)
    def repair(rid):
        barrier.wait(timeout=10)
        return issuance.repair_accepted_invoice(tenant_id=t.id, invoice_id=iid, audit_run_id=rid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(repair, runs))
    assert sum(r is not None and r["status"] == "repaired" for r in results) == 1


def test_check_api_is_rules_only_and_reuses_recent_completed_run(client):
    t = _tenant()
    result = client.post(f"{_B}/mailroom/check", headers=_auth(t))
    assert result.status_code == 200, result.text
    rid = result.json()["run_id"]
    for _ in range(100):
        run = client.get(f"{_B}/mailroom/audit/{rid}", headers=_auth(t)).json()["run"]
        if run["status"] != "running":
            break
        time.sleep(.02)
    assert run["status"] == "done", run
    again = client.post(f"{_B}/mailroom/check", headers=_auth(t)).json()
    assert again["cached"] and again["run_id"] == rid
    assert client.get(f"{_B}/mailroom/audit/{rid}", headers=_auth(_tenant())).status_code == 404


def test_start_claim_serializes_and_marks_stale_runs(monkeypatch):
    t = _tenant()
    monkeypatch.setattr(audit, "threading", SimpleNamespace(Thread=Mock()))
    with SessionLocal() as db:
        stale = OfftakerAuditRun(tenant_id=t.id, status="running",
            started_at=datetime.utcnow() - timedelta(minutes=20))
        db.add(stale); db.commit(); old_id = stale.id
    barrier = Barrier(2)
    def start(_):
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            return audit.start_run(db, t.id, rules_only=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, range(2)))
    assert results[0]["run_id"] == results[1]["run_id"]
    with SessionLocal() as db:
        assert db.get(OfftakerAuditRun, old_id).status == "failed"


def test_pagination_has_global_money_and_no_false_legacy_duplicates():
    t = _tenant(); sid = _sub(t.id)
    with SessionLocal() as db:
        a = _frozen(db, t.id, sid, "2026-06", amount=1000)
        b = _frozen(db, t.id, sid, "2026-07", amount=2000)
        a.sent_at = datetime(2026, 7, 1); b.sent_at = datetime(2026, 8, 1)
        sub = db.get(BillingReportSubscription, sid)
        sub.last_sent_at = a.sent_at; sub.last_sent_period_end = "2026-06-28"
        sub.last_sent_amount_usd = 10
        db.commit()
        first = mailroom.board(db, t.id, t, limit=1)
        second = mailroom.board(db, t.id, t, limit=1, offset=1)
        assert first["counts"]["billed_usd"] == second["counts"]["billed_usd"] == 30
        assert first["has_more"] and not second["has_more"]
        assert len(first["sent"]) == 1 and not first["sent"][0]["legacy"]


def test_retry_exhaustion_and_pause_labels():
    t, sid, iid, did = _setup(dispatch_status="failed")
    with SessionLocal() as db:
        db.get(BillingEmailDispatch, did).attempts = 8
        db.commit()
        tenant = db.get(Tenant, t.id)
        assert mailroom.outgoing_items(db, t.id, tenant)[0]["status"] == "retry_exhausted"
        tenant.sending_paused = True
        assert mailroom.outgoing_items(db, t.id, tenant)[0]["status"] == "paused"


def test_model_budget_bounds_roster_without_modifying_input():
    payload = {"subscriptions": [{"name": "x" * 1000}] * 200,
               "outgoing": [{"reason": "y" * 1000}] * 200,
               "reconcile": {"subscriptions": [{"detail": "z" * 1000}] * 200}}
    text = audit._bounded_json(payload)
    assert len(text) <= audit.MAX_PAYLOAD_CHARS
    assert len(payload["reconcile"]["subscriptions"]) == 200


def test_force_bypasses_cached_check_and_old_worker_cannot_overwrite_stale_failure(monkeypatch):
    t = _tenant()
    monkeypatch.setattr(audit, "threading", SimpleNamespace(Thread=Mock()))
    with SessionLocal() as db:
        completed = OfftakerAuditRun(tenant_id=t.id, status="done", triggered_by="mailroom_check",
            started_at=datetime.utcnow(), finished_at=datetime.utcnow())
        db.add(completed); db.commit(); old = completed.id
    with SessionLocal() as db:
        new = audit.start_run(db, t.id, rules_only=True, force=True)
    assert new["run_id"] != old and not new["cached"]
    with SessionLocal() as db:
        row = db.get(OfftakerAuditRun, new["run_id"])
        row.status = "failed"; row.error = "old lease expired"
        db.commit()
    audit.execute_run(new["run_id"], tenant_id=t.id, rules_only=True)
    with SessionLocal() as db:
        row = db.get(OfftakerAuditRun, new["run_id"])
        assert row.status == "failed" and row.error == "old lease expired"


def test_partial_audit_coverage_is_visible(monkeypatch):
    t = _tenant()
    monkeypatch.setattr(audit, "gather", lambda *a, **kw: {
        "coverage": {"checked": 1000, "total": 1200, "truncated": True},
        "tenant": {}, "sent": [], "outgoing": [], "subscriptions": []})
    rid = _run(t.id)
    audit.execute_run(rid, tenant_id=t.id, rules_only=True)
    with SessionLocal() as db:
        run = db.get(OfftakerAuditRun, rid)
        assert run.stats["coverage"]["total"] == 1200
        assert any(f["code"] == "partial_coverage" for f in run.findings)
        assert run.verdict != "ready"


def test_receipt_mapping_never_calls_delivery_delayed_delivered():
    from api.energy_agent import EaEmailDelivery, _ea_ensure_email_delivery_table
    t, sid, iid, did = _setup()
    with SessionLocal() as db:
        _ea_ensure_email_delivery_table(db)
        db.add(EaEmailDelivery(to_email="fixture@example.test", event="delivery_delayed",
            resend_email_id="delayed-only", created_at=datetime.utcnow()))
        db.add(EaEmailDelivery(to_email="fixture@example.test", event="delivered",
            resend_email_id="actually-delivered", created_at=datetime.utcnow()))
        db.commit()
        events = mailroom._delivery_events(db, {"delayed-only", "actually-delivered"})
        assert events["delayed-only"]["status"] is None
        assert events["actually-delivered"]["status"] == "delivered"


def test_foreign_email_override_cannot_be_consumed_by_repair():
    t, sid, iid, did = _setup()
    foreign = _tenant()
    with SessionLocal() as db:
        override = EmailCopyOverride(tenant_id=foreign.id, channel="offtaker",
            max_sends=1, sends_used=0, status="active")
        db.add(override); db.flush()
        inv = db.get(OfftakerInvoice, iid)
        inv.render_snapshot = {**(inv.render_snapshot or {}), "email_copy_override_id": override.id}
        db.commit(); override_id = override.id
    rid = _run(t.id)
    audit.execute_run(rid, tenant_id=t.id, rules_only=True)
    with SessionLocal() as db:
        override = db.get(EmailCopyOverride, override_id)
        assert override.sends_used == 0 and override.status == "active"
        assert db.get(OfftakerInvoice, iid).applied_at is None
        assert db.get(OfftakerInvoice, iid).status == "failed"
        stats = db.get(OfftakerAuditRun, rid).stats
        assert stats["repaired_count"] == 0
        assert stats["repairs"][0]["status"] == "failed"


@pytest.mark.parametrize("amount,payment_status,paid_amount,refund,offline,status", [
    (1000, None, 0, 0, 0, "accepted"),
    (0, None, 0, 0, 0, "accepted"),
    (0, "open", 0, 0, 0, "accepted"),
    (1000, "open", 1000, 0, 0, "accepted"),
    (1000, "paid", 1000, 0, 0, "accepted"),
    (1000, "refunded", 1000, 1000, 0, "accepted"),
    (1000, None, 0, 0, 400, "accepted"),
    (1000, None, 0, 0, 1000, "accepted"),
    (1000, "open", 1000, 0, 0, "uncertain"),
])
def test_global_payment_counts_match_row_summary(amount, payment_status, paid_amount, refund, offline, status):
    t = _tenant(); sid = _sub(t.id)
    with SessionLocal() as db:
        inv = _frozen(db, t.id, sid, "2026-06", amount=amount, status=status)
        if payment_status is not None:
            payment = OfftakerPayment(tenant_id=t.id, subscription_id=sid,
                invoice_number="2026-06", period_key="2026-06", amount_cents=paid_amount,
                fee_cents=0, status=payment_status, refunded_cents=refund)
            db.add(payment); db.flush(); inv.payment_id = payment.id
        if offline:
            from datetime import date
            db.add(OfftakerSettlement(tenant_id=t.id, invoice_id=inv.id, subscription_id=sid,
                amount_cents=offline, request_key="mailroom-counts", method="check",
                received_on=date(2026, 7, 1), actor="fixture", note="Recorded fixture check"))
        db.commit()
        rows, _ = mailroom.sent_items(db, t.id)
        summary = rows[0]["payment_summary"]
        counts = mailroom.portfolio_counts(db, t.id)
        assert counts["paid"] == int(summary == "paid")
        assert counts["unpaid"] == int(summary in ("partial", "unpaid"))


def test_late_trueup_finish_never_rewinds_window_metadata():
    from datetime import date
    t, sid, iid, did = _setup()
    with SessionLocal() as db:
        inv = db.get(OfftakerInvoice, iid)
        inv.snapshot = {"computed_invoice": {"is_trueup": True, "credit_usd": 0}}
        sub = db.get(BillingReportSubscription, sid)
        sub.last_trueup_window_end = date(2026, 8, 31)
        sub.last_sent_at = datetime(2026, 9, 1)
        db.commit()
    issuance.finish(iid, {"ok": True, "accepted_at": datetime(2026, 7, 1)})
    with SessionLocal() as db:
        assert db.get(BillingReportSubscription, sid).last_trueup_window_end == date(2026, 8, 31)


def test_sent_reader_batches_heavy_envelopes_and_keeps_full_coverage(monkeypatch):
    t = _tenant(); sid = _sub(t.id)
    subscription_ids = [sid] + [_sub(t.id) for _ in range(110)]
    with SessionLocal() as db:
        # Different subscriptions allow the same period without touching money.
        for subscription_id in subscription_ids:
            inv = _frozen(db, t.id, subscription_id, "2026-06")
            inv.render_snapshot = {"variants": {"offline": {"html": "x" * 10000}}}
        db.commit()
    original = mailroom._sent_items_page
    observed = []
    def page(db, tenant_id, ctx, invoice_ids):
        observed.append(len(invoice_ids))
        result = original(db, tenant_id, ctx, invoice_ids)
        # The helper returns summaries, never the heavyweight frozen body.
        assert all("x" * 10000 not in str(item) for item in result)
        return result
    monkeypatch.setattr(mailroom, "_sent_items_page", page)
    with SessionLocal() as db:
        rows, total = mailroom.sent_items(db, t.id, limit=1000)
        assert len(rows) == total == 111
    assert observed == [50, 50, 11]
