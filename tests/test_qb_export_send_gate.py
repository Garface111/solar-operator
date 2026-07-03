"""QB/Xero invoice register mirrors the send gate (caught at 800-offtaker scale).

The export's contract is "current-period offtaker invoices ... never a
fabricated row". Before this gate, a manual offtaker with NO settled utility
bill (unbound, or bill not landed) exported a NON-ZERO telemetry-derived row —
a receivable the send pipeline itself would refuse to email — and disabled
subscriptions exported too. The register must contain exactly the invoices the
system would actually send.
"""
from __future__ import annotations

import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_qb_gate_test")

import secrets
from datetime import datetime

from api.db import SessionLocal, init_db
from api.models import (Tenant, Array, UtilityAccount, Bill,
                        BillingReportSubscription)
from api.billing.qb_export import build_invoice_register


def _mk_bill(db, tid, acct_id, excess, credit):
    db.add(Bill(tenant_id=tid, account_id=acct_id,
                period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 30),
                kwh_generated=int(excess), kwh_sent_to_grid=float(excess),
                solar_credit_usd=credit, is_net_metered=True,
                parse_status="parsed"))


def _cleanup(db, tid):
    """Remove this test's rows — some legacy tests assert on WHOLE tables, so
    leftover rows from this file would order-dependently break them."""
    from api.models import ReportDraft
    for model in (ReportDraft, BillingReportSubscription, Bill,
                  UtilityAccount, Array):
        db.query(model).filter(model.tenant_id == tid).delete(
            synchronize_session=False)
    t = db.get(Tenant, tid)
    if t is not None:
        db.delete(t)
    db.commit()


def test_register_excludes_unsendable_and_disabled():
    init_db()
    with SessionLocal() as db:
        tid = "ten_" + secrets.token_hex(4)
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="QG",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Gate Array", region="VT")
        db.add(arr)
        db.flush()
        host = UtilityAccount(tenant_id=tid, provider="gmp", array_id=arr.id,
                              account_number="HOST-GATE")
        db.add(host)
        db.flush()
        _mk_bill(db, tid, host.id, 10000, 1600.0)   # host/group bill

        own = UtilityAccount(tenant_id=tid, provider="gmp",
                             account_number="OWN-GATE")
        db.add(own)
        db.flush()
        _mk_bill(db, tid, own.id, 500, 80.0)        # offtaker's settled bill

        # (a) bill-bound + settled bill → the ONE exportable invoice.
        db.add(BillingReportSubscription(
            tenant_id=tid, customer_name="Billable Offtaker", array_id=arr.id,
            allocation_pct=1.0, array_share_pct=0.05, utility_account_id=own.id,
            billing_model="percent_of_array", cadence="monthly", enabled=True))
        # (b) unbound (array-only): non-zero telemetry-ish figure, but the send
        #     gate refuses it — must NOT export.
        db.add(BillingReportSubscription(
            tenant_id=tid, customer_name="Unbound Offtaker", array_id=arr.id,
            allocation_pct=0.10,
            billing_model="percent_of_array", cadence="monthly", enabled=True))
        # (c) disabled but otherwise billable — must NOT export.
        db.add(BillingReportSubscription(
            tenant_id=tid, customer_name="Disabled Offtaker", array_id=arr.id,
            allocation_pct=1.0, array_share_pct=0.04, utility_account_id=own.id,
            billing_model="percent_of_array", cadence="monthly", enabled=False))
        db.commit()

        try:
            csv_text, count = build_invoice_register(db, tid, fmt="xero")
        finally:
            _cleanup(db, tid)

    assert count == 1, f"expected exactly the billable row, got {count}:\n{csv_text}"
    assert "Billable Offtaker" in csv_text
    assert "Unbound Offtaker" not in csv_text
    assert "Disabled Offtaker" not in csv_text


def test_bill_bound_legacy_flat_rate_is_the_price():
    """A bill-bound offtaker with a legacy flat rate_per_kwh bills at THAT rate
    with no re-discount (the flat rate encodes the agreed price). Before the
    800-scale catch, it billed the BILL's credit rate paired with the flat
    rule's zero discount — neither semantic."""
    from api.billing.delivery import build_match

    init_db()
    with SessionLocal() as db:
        tid = "ten_" + secrets.token_hex(4)
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="FL",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Flat Rate Array", region="VT")
        db.add(arr)
        db.flush()
        host = UtilityAccount(tenant_id=tid, provider="gmp", array_id=arr.id,
                              account_number="HOST-FL")
        db.add(host)
        db.flush()
        _mk_bill(db, tid, host.id, 10000, 1700.0)   # bill credit rate 0.17
        own = UtilityAccount(tenant_id=tid, provider="gmp",
                             account_number="OWN-FL")
        db.add(own)
        db.flush()
        _mk_bill(db, tid, own.id, 500, 85.0)
        sub = BillingReportSubscription(
            tenant_id=tid, customer_name="Flat Rate Offtaker", array_id=arr.id,
            allocation_pct=1.0, array_share_pct=0.05, utility_account_id=own.id,
            rate_per_kwh=0.145,   # the agreed legacy flat price
            billing_model="percent_of_array", cadence="monthly", enabled=True)
        db.add(sub)
        db.commit()
        try:
            ci = build_match(sub).computed_invoice or {}
            # real-math kwh = 0.05 × 10000 = 500; amount = 500 × 0.145 exactly.
            assert ci.get("net_rate_per_kwh") == 0.145, ci.get("net_rate_per_kwh")
            assert ci.get("discount_pct") == 0.0, ci.get("discount_pct")
            assert abs(ci.get("amount_owed") - 72.50) < 0.011, ci.get("amount_owed")
        finally:
            _cleanup(db, tid)


def test_draft_not_recreated_after_period_sent(monkeypatch):
    """Exactly-once for DRAFTS (caught at 800-offtaker scale): after a period is
    approved+sent, the scheduler tick must NOT re-draft a phantom 'ready to
    review' for that same period — it skips (already_sent) until a new bill
    lands. Without the guard, Anna-scale operators got hundreds of phantom
    drafts on the 1st of every month."""
    import api.notify as notify
    from api.billing.delivery import draft_subscription, deliver_subscription
    from api.models import ReportDraft, Tenant as T

    monkeypatch.setattr(notify, "_send_via_resend",
                        lambda *a, **k: True)

    init_db()
    with SessionLocal() as db:
        tid = "ten_" + secrets.token_hex(4)
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="DG",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Draft Guard Array", region="VT")
        db.add(arr)
        db.flush()
        host = UtilityAccount(tenant_id=tid, provider="gmp", array_id=arr.id,
                              account_number="HOST-DG")
        db.add(host)
        db.flush()
        _mk_bill(db, tid, host.id, 9000, 1500.0)
        own = UtilityAccount(tenant_id=tid, provider="gmp",
                             account_number="OWN-DG")
        db.add(own)
        db.flush()
        _mk_bill(db, tid, own.id, 450, 75.0)
        sub = BillingReportSubscription(
            tenant_id=tid, customer_name="Draft Guard Offtaker", array_id=arr.id,
            allocation_pct=1.0, array_share_pct=0.05, utility_account_id=own.id,
            billing_model="percent_of_array", cadence="monthly",
            delivery_mode="approval", send_mode="to_me",
            operator_email="op@e.com", enabled=True)
        db.add(sub)
        db.commit()

        tenant = db.get(T, tid)
        r1 = draft_subscription(db, sub, tenant, triggered_by="test")
        assert r1.get("ok"), r1
        # Approve → real send path stamps last_sent_period_end.
        r2 = deliver_subscription(db, sub, tenant, triggered_by="test-approve")
        assert r2.get("ok"), r2
        db.refresh(sub)
        assert sub.last_sent_period_end
        # Mark the reviewed draft sent, as the approve endpoint does.
        for d in db.query(ReportDraft).filter(
                ReportDraft.subscription_id == sub.id):
            d.status = "sent"
        db.commit()

        # The next tick must SKIP, not re-draft the same period.
        try:
            r3 = draft_subscription(db, sub, tenant, triggered_by="test-tick2")
            assert r3.get("skipped") and r3.get("already_sent"), r3
            pending = db.query(ReportDraft).filter(
                ReportDraft.subscription_id == sub.id,
                ReportDraft.status == "pending").count()
            assert pending == 0
        finally:
            _cleanup(db, tid)
