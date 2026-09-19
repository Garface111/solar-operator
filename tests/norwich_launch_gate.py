"""Explicit NO-GO acceptance gate. Intentionally outside default test_ discovery.
Run: python -m pytest tests/norwich_launch_gate.py -q
These are unmet requirements, not xfails or waived tests. No external effects.
"""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock
import pytest
from sqlalchemy import select
from api.db import SessionLocal
from api.models import Tenant, BillingReportSubscription, OfftakerPayment
from api.billing import delivery, payments as pay
from tests.test_offtaker_payments import _tenant, _sub, _FakeMatch


def test_historical_summary_survives_next_invoice():
    from api.billing.monthly_report import collect_rows
    t=_tenant(); sid=_sub(t.id)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid)
        sub.last_sent_period_end="2026-07-31"; sub.last_sent_at=datetime(2026,8,1)
        sub.last_sent_amount_usd=200
        db.add(OfftakerPayment(tenant_id=t.id,subscription_id=sid,period_key="2026-06-30",invoice_number="JUNE",amount_cents=10000,fee_cents=50,status="paid",paid_at=datetime(2026,7,2)))
        db.commit()
        rows=collect_rows(db,db.get(Tenant,t.id),"2026-06")
        assert len(rows)==1, "June invoice disappeared after July was sent"
        assert rows[0]["billed_usd"]==100


def test_partial_refund_reduces_reported_collections():
    from api.billing.invoice_ledger import list_payment_rows
    t=_tenant();sid=_sub(t.id)
    with SessionLocal() as db:
        row=OfftakerPayment(tenant_id=t.id,subscription_id=sid,period_key="2026-06-30",invoice_number="JUNE",amount_cents=10000,fee_cents=50,status="paid",stripe_payment_intent_id="pi_partial_"+t.id)
        db.add(row);db.commit()
        pay.mark_payment_refunded(db,charge_dict={"payment_intent":row.stripe_payment_intent_id,"amount_refunded":4000,"refunded":False})
        values=list_payment_rows(db,db.get(BillingReportSubscription,sid))
        assert values[0]["collected_usd"]<=60, values


def test_partial_vec_month_is_not_billable():
    from tests.test_vec_offtaker_billing import _seed
    tid,aid,ua=_seed(with_generation=True)
    from api.models import DailyGeneration
    from datetime import date
    from sqlalchemy import delete
    with SessionLocal() as db:
        db.execute(delete(DailyGeneration).where(DailyGeneration.array_id == aid,
                   DailyGeneration.day > date(2026, 5, 10)))
        db.commit()
    sub=BillingReportSubscription(tenant_id=tid,customer_name="Partial month",utility_account_id=ua,array_id=aid,allocation_pct=.4,net_rate_per_kwh=.25,discount_pct=.1,billing_model="percent_of_array")
    result=delivery.build_manual_match(sub)
    assert result.computed_invoice["has_utility_bill"] is False, "Ten days of May were treated as a billable month"


def test_overallocated_batch_does_not_partially_create_roster(client):
    from tests.test_offtaker_upload import _make_tenant,_make_array_with_bill
    tid,auth=_make_tenant();aid,ua=_make_array_with_bill(tid,"105 percent audit","GMP-105",with_bill=True)
    rows=[{"offtaker_name":f"Owner {i}","array_id":aid,"utility_account_id":ua,"allocation_pct":.35,"email":f"owner{i}@example.test"} for i in range(3)]
    response=client.post("/v1/array-operator/billing/subscriptions/bulk-commit",headers={"Authorization":auth},json={"rows":rows})
    with SessionLocal() as db:
        created=db.execute(select(BillingReportSubscription).where(BillingReportSubscription.tenant_id==tid)).scalars().all()
        assert len(created)==0, f"Invalid 105% batch created {len(created)} live rows: {response.text}"


def test_quarterly_trueup_uses_actual_invoiced_budget(monkeypatch):
    from api.billing.trueup import compute_annual_trueup
    from datetime import date
    t=_tenant();sid=_sub(t.id,cadence="quarterly",budget_amount_usd=100,annual_trueup=True)
    match=_FakeMatch(amount=100)
    match.computed_invoice.update(budget_override=True,solar_credit_value=50)
    monkeypatch.setattr(delivery,"build_match",lambda *a,**kw:match)
    with SessionLocal() as db:
        for month in (11,2,5,8):
            year=2025 if month==11 else 2026
            db.add(OfftakerPayment(tenant_id=t.id,subscription_id=sid,period_key=f"{year}-{month:02d}-28",invoice_number=f"Q-{month}",amount_cents=10000,fee_cents=50,status="paid",paid_at=datetime(year,month,28)))
        db.commit()
        result=compute_annual_trueup(db.get(BillingReportSubscription,sid),as_of=date(2026,9,1))
    assert result.total_budgeted==400, result.to_dict()
    assert result.charge_usd==200  # actual $600 minus four $100 invoices


def _delivery_setup(client,monkeypatch):
    from tests.test_billing_delivery import _make_tenant,_upload
    tid,auth=_make_tenant()
    sid=_upload(client,auth,"norwich.xlsx").json()["subscription"]["id"]
    monkeypatch.setattr(pay,"link_existing_connect_account",lambda *a:{})
    monkeypatch.setattr(pay,"refresh_connect_status",lambda *a:{})
    monkeypatch.setattr(pay,"create_offtaker_payment",lambda *a,**kw:{"ok":False,"skipped":True})
    monkeypatch.setattr(delivery,"generate_files",lambda *a,**kw:[])
    return tid,sid


def test_concurrent_send_paths_only_send_once(client,monkeypatch):
    tid,sid=_delivery_setup(client,monkeypatch)
    barrier=Barrier(2); sends=[]
    def mail(**kwargs):
        sends.append(kwargs);return True
    monkeypatch.setattr("api.notify._send_via_resend",mail)
    def deliver():
        barrier.wait(timeout=10)
        with SessionLocal() as db:
            return delivery.deliver_subscription(db,db.get(BillingReportSubscription,sid),db.get(Tenant,tid))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(deliver) for _ in range(2)]
        outcomes=[f.result() for f in futures]
    assert len(sends)==1, f"Concurrent paths sent {len(sends)} invoices: {outcomes}"


def test_crash_after_mail_acceptance_does_not_resend(client,monkeypatch):
    tid,sid=_delivery_setup(client,monkeypatch); sends=[]
    def mail(**kwargs):
        sends.append(kwargs); return True
    monkeypatch.setattr("api.notify._send_via_resend",mail)
    with SessionLocal() as db:
        tenant=db.get(Tenant,tid);sub=db.get(BillingReportSubscription,sid)
        original_commit = db.commit
        def commit_after_acceptance():
            if sends:
                raise RuntimeError("database unavailable after mail accepted")
            return original_commit()
        monkeypatch.setattr(db, "commit", commit_after_acceptance)
        with pytest.raises(RuntimeError):
            delivery.deliver_subscription(db,sub,tenant)
        db.rollback()
    with SessionLocal() as db:
        delivery.deliver_subscription(db,db.get(BillingReportSubscription,sid),db.get(Tenant,tid))
    assert len(sends)==1, f"Retry after a commit failure sent {len(sends)} invoices"
