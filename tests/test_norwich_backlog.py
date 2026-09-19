from datetime import date, datetime
from api.db import SessionLocal
from api.models import BillingReportSubscription, OfftakerInvoice, ReportDraft
from api.billing.backlog import queue_closed_periods
from tests.test_offtaker_payments import _tenant, _sub


def test_missed_months_persist_before_newest_bill_and_are_not_lost():
    t=_tenant(); sid=_sub(t.id)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid)
        sub.created_at=datetime(2026,6,1)
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,9,19)) == ["2026-06","2026-07","2026-08"]
        records=db.query(OfftakerInvoice).filter_by(subscription_id=sid).all()
        assert len(records)==3
        assert all(r.status == "held" and r.last_error for r in records)
        records[-1].status="accepted"; db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,9,19)) == ["2026-06","2026-07"]
        assert db.query(OfftakerInvoice).filter_by(subscription_id=sid).count()==3


def test_new_import_never_backbills_pre_onboarding_or_open_period():
    t=_tenant(); sid=_sub(t.id)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid); sub.created_at=datetime(2026,9,19)
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,9,19)) == []
        assert queue_closed_periods(db,sub,today=date(2026,10,1)) == ["2026-09"]


def test_quarterly_enumerates_only_closed_quarters():
    t=_tenant(); sid=_sub(t.id,cadence="quarterly")
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid); sub.created_at=datetime(2026,1,1)
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,9,19)) == ["2026-Q1","2026-Q2"]


def test_pending_draft_does_not_regenerate_each_daily_tick():
    t=_tenant(); sid=_sub(t.id)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid); sub.created_at=datetime(2026,6,1)
        db.add(ReportDraft(tenant_id=t.id,subscription_id=sid,customer_name="Pending",
            period_label="2026-06-01 → 2026-06-30",status="pending"))
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,7,1)) == []


def test_legacy_last_sent_extends_gap_discovery_without_resending():
    t=_tenant(); sid=_sub(t.id)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid); sub.created_at=datetime(2026,9,1)
        sub.last_sent_period_end="2026-06-30"
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,9,19)) == ["2026-07","2026-08"]


def test_scheduler_passes_every_closed_period_to_delivery(monkeypatch):
    from api import scheduler
    from api.billing import backlog, delivery
    t=_tenant(); sid=_sub(t.id,delivery_mode="auto")
    monkeypatch.setattr(backlog,"queue_closed_periods",lambda db,sub: ["2026-06","2026-07"] if sub.id == sid else [])
    monkeypatch.setattr(scheduler,"_auto_send_should_hold",lambda *a,**kw:False)
    monkeypatch.setattr(scheduler,"_unconfirmed_rate_should_hold",lambda *a,**kw:False)
    seen=[]
    monkeypatch.setattr(delivery,"deliver_subscription",lambda db,sub,tenant,**kw: seen.append((sub.id,kw["period_label"])) or {"ok":True})
    monkeypatch.setattr(scheduler,"send_internal_alert",lambda *a,**kw:None)
    scheduler.deliver_billing_reports("monthly")
    assert seen == [(sid,"2026-06"),(sid,"2026-07")]


def test_scheduler_verification_errors_hold_instead_of_sending(monkeypatch):
    from api import scheduler
    from api.billing import delivery
    def broken(*a,**kw):
        raise RuntimeError("verification unavailable")
    monkeypatch.setattr(delivery,"build_manual_match",broken)
    from types import SimpleNamespace
    assert scheduler._unconfirmed_rate_should_hold(None,SimpleNamespace(source_workbook=None))


def test_review_job_honors_pause_mode_and_already_sent(monkeypatch):
    from api.jobs import new_bill_review
    from api.billing import delivery
    from tests.test_draft_period_selector import _seed_multi_period_offtaker
    tid,auth,sid=_seed_multi_period_offtaker()
    from api.models import Tenant
    from unittest.mock import Mock
    draft=Mock(return_value=None)
    monkeypatch.setattr(new_bill_review,"_ensure_draft",draft)
    monkeypatch.setattr(delivery,"_utility_bill_period_kwh",lambda *a,**kw:(1,date(2026,6,1),date(2026,6,30),"2026-06"))
    with SessionLocal() as db:
        tenant=db.get(Tenant,tid);tenant.sending_paused=True;db.commit()
    new_bill_review.run_new_bill_reviews(dry_run=True)
    assert all(call.args[1].id != sid for call in draft.call_args_list)
    with SessionLocal() as db:
        db.get(Tenant,tid).sending_paused=False
        db.get(BillingReportSubscription,sid).delivery_mode="auto";db.commit()
    draft.reset_mock();new_bill_review.run_new_bill_reviews(dry_run=True)
    assert all(call.args[1].id != sid for call in draft.call_args_list)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid);sub.delivery_mode="approval"
        sub.last_sent_period_end="2026-06-30";db.commit()
    draft.reset_mock();new_bill_review.run_new_bill_reviews(dry_run=True)
    assert all(call.args[1].id != sid for call in draft.call_args_list)


def test_minted_payment_does_not_hide_failed_invoice_retry():
    from api.models import OfftakerPayment
    t=_tenant();sid=_sub(t.id)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid);sub.created_at=datetime(2026,6,1)
        db.add(OfftakerInvoice(tenant_id=t.id,subscription_id=sid,period_key="2026-06",
            period_start=date(2026,6,1),period_end=date(2026,6,30),status="failed",snapshot={}))
        db.add(OfftakerPayment(tenant_id=t.id,subscription_id=sid,period_key="2026-06-30",
            invoice_number="June",amount_cents=10000,fee_cents=0,status="pending"))
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,7,1)) == ["2026-06"]


def test_changed_cadence_does_not_queue_overlapping_issued_quarter():
    t=_tenant();sid=_sub(t.id,cadence="monthly")
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid);sub.created_at=datetime(2026,7,1)
        db.add(OfftakerInvoice(tenant_id=t.id,subscription_id=sid,period_key="2026-Q2",
            period_start=date(2026,4,1),period_end=date(2026,6,30),status="accepted",snapshot={}))
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2026,8,1)) == ["2026-07"]
        assert db.query(OfftakerInvoice).filter_by(subscription_id=sid).count()==2


def test_real_historical_delivery_retries_older_gap_after_newer_invoice(monkeypatch):
    from tests.test_draft_period_selector import _seed_multi_period_offtaker
    from api.models import Tenant, BillingEmailDispatch
    from api.billing import delivery, payments
    tid,auth,sid=_seed_multi_period_offtaker()
    monkeypatch.setattr(delivery,"generate_files",lambda *a,**kw:[])
    monkeypatch.setattr(payments,"create_offtaker_payment",lambda *a,**kw:{"ok":False})
    monkeypatch.setattr(payments,"link_existing_connect_account",lambda *a,**kw:{})
    monkeypatch.setattr(payments,"refresh_connect_status",lambda *a,**kw:{})
    calls=[]
    def mail(**kw):
        calls.append(kw)
        return len(calls)>1
    monkeypatch.setattr("api.notify._send_via_resend",mail)
    with SessionLocal() as db:
        sub=db.get(BillingReportSubscription,sid);sub.created_at=datetime(2025,10,1)
        tenant=db.get(Tenant,tid);tenant.offtaker_payment_policy="offline"
        db.commit()
        periods=queue_closed_periods(db,sub,today=date(2025,12,1))
        assert periods==["2025-10","2025-11"]
        assert not delivery.deliver_subscription(db,sub,tenant,period_label=periods[0])["ok"]
        assert delivery.deliver_subscription(db,sub,tenant,period_label=periods[1])["ok"]
        for dispatch in db.query(BillingEmailDispatch).filter_by(tenant_id=tid):
            dispatch.retry_at=None
        db.commit()
        assert queue_closed_periods(db,sub,today=date(2025,12,1))==["2025-10"]
        assert delivery.deliver_subscription(db,sub,tenant,period_label="2025-10")["ok"]
        assert queue_closed_periods(db,sub,today=date(2025,12,1))==[]
        invoices=db.query(OfftakerInvoice).filter_by(subscription_id=sid).all()
        assert len(invoices)==2 and all(i.status=="accepted" for i in invoices)
    assert len(calls)==3
