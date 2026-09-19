"""Failure injection at real durable transaction boundaries, with mocked transport only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier
from sqlalchemy import select
import pytest
from api.db import SessionLocal
from api.models import BillingEmailDispatch, OfftakerInvoice, OfftakerPayment, Tenant, BillingReportSubscription
from api.billing import dispatch, issuance, delivery
from api.billing.matcher import BillingMatch, Period
from tests.test_billing_delivery import _make_tenant, _upload


def _match(month, amount=40):
    return BillingMatch(matched=True, confidence=1, source="schema",
        customer={"name":"Fixture"}, latest_period=Period(start=date(2026,month,1),end=date(2026,month,28)),
        computed_invoice={"period_start":f"2026-{month:02d}-01", "period_end":f"2026-{month:02d}-28",
                          "amount_owed":amount,"invoice_number":str(month),"kwh":100})


def test_concurrent_periods_cannot_spend_same_credit(client):
    tid, auth = _make_tenant()
    sid = _upload(client,auth).json()["subscription"]["id"]
    with SessionLocal() as db:
        db.get(BillingReportSubscription,sid).pending_credit_usd=50
        db.commit()
    barrier=Barrier(2)
    def issue(month):
        barrier.wait(timeout=10)
        return issuance.freeze(tenant_id=tid,subscription_id=sid,key=f"2026-{month:02d}",match=_match(month))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(issue,[6,7]))
    with SessionLocal() as db:
        rows=db.scalars(select(OfftakerInvoice).where(OfftakerInvoice.tenant_id==tid)).all()
        assert sum(r.credit_applied_cents for r in rows)==5000
        assert sum(r.amount_cents for r in rows)==3000
        assert db.get(BillingReportSubscription,sid).pending_credit_usd==0
    again=issuance.freeze(tenant_id=tid,subscription_id=sid,key="2026-06",match=_match(6,99))
    assert again[1].computed_invoice["amount_owed"] in (0,30)


def test_receipt_commit_failure_keeps_send_uncertain_permanently(monkeypatch):
    tid,_=_make_tenant(); sends=[]
    real_factory=dispatch.SessionLocal
    def mail(**kw): sends.append(kw);return True
    monkeypatch.setattr("api.notify._send_via_resend",mail)
    def sessions():
        db=real_factory();original=db.commit
        def commit():
            if sends: raise RuntimeError("receipt commit lost")
            return original()
        db.commit=commit
        return db
    monkeypatch.setattr(dispatch,"SessionLocal",sessions)
    with pytest.raises(RuntimeError,match="receipt commit lost"):
        dispatch.send_email_once(tenant_id=tid,key="receipt-loss",email={"to":"fixture@example.test","subject":"one","html":"one"})
    monkeypatch.setattr(dispatch,"SessionLocal",real_factory)
    retry=dispatch.send_email_once(tenant_id=tid,key="receipt-loss",email={"to":"different@example.test","subject":"different","html":"two"})
    assert retry["uncertain"] and not retry["ok"] and len(sends)==1


def test_real_sdk_rate_limit_is_retryable_and_obeys_provider_delay(monkeypatch):
    from api import notify, email_archive
    import resend
    from resend.exceptions import RateLimitError
    tid,_=_make_tenant(); attempts=[]
    monkeypatch.setattr(notify,"RESEND_API_KEY","test-key-not-real")
    monkeypatch.setattr(email_archive,"record",lambda **kw:None)
    def send(params, options=None):
        attempts.append((params,options))
        if len(attempts)==1:
            raise RateLimitError("slow down","rate_limit_exceeded","429",{"retry-after":"180"})
        return {"id":"fixture-provider-receipt"}
    monkeypatch.setattr(resend.Emails,"send",send)
    email={"to":"fixture@example.test","subject":"Frozen","html":"First"}
    first=dispatch.send_email_once(tenant_id=tid,key="rate-limit",email=email)
    assert not first["ok"] and not first["uncertain"]
    second=dispatch.send_email_once(tenant_id=tid,key="rate-limit",email={**email,"html":"Changed"})
    assert not second["ok"] and len(attempts)==1
    with SessionLocal() as db:
        row=db.scalar(select(BillingEmailDispatch).where(BillingEmailDispatch.tenant_id==tid))
        assert row.status=="failed" and row.retry_at >= datetime.utcnow()+timedelta(seconds=170)
        row.retry_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
    results=dispatch.retry_due_dispatches()
    assert any(r.get("ok") and r["key"]=="rate-limit" for r in results)
    assert len(attempts)==2 and attempts[0]==attempts[1]


def test_invoice_payment_link_survives_crash_before_finish(client,monkeypatch):
    from api.billing import payments
    tid,auth=_make_tenant();sid=_upload(client,auth,"norwich.xlsx").json()["subscription"]["id"]
    with SessionLocal() as db:
        db.get(Tenant,tid).offtaker_payment_policy="online_required";db.commit()
    monkeypatch.setattr(payments,"link_existing_connect_account",lambda *a: {})
    monkeypatch.setattr(payments,"refresh_connect_status",lambda *a: {})
    def payment(db,*,tenant,sub,match,**kw):
        row=OfftakerPayment(tenant_id=tid,subscription_id=sid,invoice_number=match.computed_invoice["invoice_number"],
            period_key=match.computed_invoice["period_end"],amount_cents=issuance.cents(match.computed_invoice["amount_owed"]),
            status="open",pay_url="https://example.test/pay/frozen",pay_token="frozen-"+tid)
        db.add(row);db.commit()
        return {"ok":True,"pay_url":row.pay_url,"payment_id":row.id}
    monkeypatch.setattr(payments,"create_offtaker_payment",payment)
    monkeypatch.setattr(delivery,"generate_files",lambda *a,**kw:[])
    sends=[];monkeypatch.setattr("api.notify._send_via_resend",lambda **kw:sends.append(kw) or True)
    real_finish=issuance.finish
    monkeypatch.setattr(issuance,"finish",lambda *a,**kw:(_ for _ in ()).throw(RuntimeError("crash before finish")))
    with SessionLocal() as db:
        with pytest.raises(RuntimeError,match="crash before finish"):
            delivery.deliver_subscription(db,db.get(BillingReportSubscription,sid),db.get(Tenant,tid))
    monkeypatch.setattr(issuance,"finish",real_finish)
    with SessionLocal() as db:
        inv=db.scalar(select(OfftakerInvoice).where(OfftakerInvoice.tenant_id==tid))
        assert inv.payment_id is not None
        iid=inv.id;pid=inv.payment_id
    assert issuance.reconcile(iid)["ok"]
    with SessionLocal() as db:
        inv=db.get(OfftakerInvoice,iid)
        assert inv.status=="accepted" and inv.payment_id==pid
    assert len(sends)==1
