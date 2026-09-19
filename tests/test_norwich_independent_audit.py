"""Independent Norwich launch regressions; all external effects are mocked."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest
from api.db import SessionLocal
from api.models import Tenant, BillingReportSubscription, OfftakerPayment
from api.billing import delivery, payments as pay
from tests.test_offtaker_payments import _tenant, _sub, _FakeMatch
from tests.test_offtaker_pay_link_durable import _mint, _fake_create


def test_operator_test_send_cannot_mint_payable_invoice(client, monkeypatch):
    from tests.test_billing_delivery import _make_tenant, _upload
    tid, auth = _make_tenant()
    sid = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    mint = MagicMock(return_value={"ok": True, "pay_url": "https://unsafe.example/pay"})
    monkeypatch.setattr(pay, "create_offtaker_payment", mint)
    monkeypatch.setattr(pay, "link_existing_connect_account", lambda *a: {})
    monkeypatch.setattr(pay, "refresh_connect_status", lambda *a: {})
    monkeypatch.setattr(delivery, "generate_files", lambda *a, **kw: [])
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    with SessionLocal() as db:
        result = delivery.deliver_subscription(db, db.get(BillingReportSubscription,sid), db.get(Tenant,tid), is_test=True)
        assert result["ok"], result
        assert db.get(BillingReportSubscription,sid).last_sent_at is None
    mint.assert_not_called()
    assert result["pay_url"] is None


def test_connect_email_collision_never_links_another_tenant(monkeypatch):
    tenant = _tenant(stripe_connect_account_id=None, stripe_connect_charges_enabled=False, contact_email="shared@example.test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    with patch.object(pay.stripe.Account, "list", return_value={"data":[{"id":"acct_other", "email":"shared@example.test", "metadata":{"tenant_id":"ten_other"}, "charges_enabled":True}], "has_more":False}):
        with SessionLocal() as db:
            t = db.get(Tenant,tenant.id)
            result = pay.link_existing_connect_account(db,t)
            assert not result.get("linked"), result
            assert t.stripe_connect_account_id is None


@pytest.mark.parametrize("status", ["complete", "unreadable"])
def test_uncertain_or_pending_checkout_never_creates_second_charge(monkeypatch,status):
    calls=[]
    t,sid,res = _mint(monkeypatch,calls)
    retrieve = MagicMock(return_value={"id":"cs_test_1","status":"complete","payment_status":"unpaid"})
    if status == "unreadable":
        retrieve.side_effect = RuntimeError("Stripe timed out")
    with patch.object(pay.stripe.checkout.Session,"retrieve",retrieve), patch.object(pay.stripe.checkout.Session,"expire",side_effect=RuntimeError("cannot expire")), patch.object(pay.stripe.checkout.Session,"create",side_effect=_fake_create(calls)):
        with SessionLocal() as db:
            result=pay.resolve_pay_link(db,res["pay_url"].rsplit("/",1)[-1])
    assert result["action"] == "unavailable", result
    assert len(calls)==1


def test_stale_paid_checkout_is_reconciled_before_replacement(monkeypatch):
    calls=[]
    t,sid,res = _mint(monkeypatch,calls)
    with SessionLocal() as db:
        db.get(OfftakerPayment,res["payment_id"]).checkout_expires_at=datetime.utcnow()-timedelta(days=1)
        db.commit()
    session={"id":"cs_test_1", "status":"complete", "payment_status":"paid", "amount_total":10000,"currency":"usd", "metadata":{"kind":"offtaker_invoice","payment_id":str(res["payment_id"])}}
    with patch.object(pay.stripe.checkout.Session,"retrieve",return_value=session), patch.object(pay.stripe.checkout.Session,"expire",side_effect=RuntimeError("already complete")), patch.object(pay.stripe.checkout.Session,"create",side_effect=_fake_create(calls)):
        with SessionLocal() as db:
            result=pay.resolve_pay_link(db,res["pay_url"].rsplit("/",1)[-1])
    assert result["action"] == "paid", result
    assert len(calls)==1


def test_force_resend_same_amount_reuses_payment(monkeypatch):
    calls=[]
    t,sid,res = _mint(monkeypatch,calls)
    with patch.object(pay.stripe.checkout.Session,"create",side_effect=_fake_create(calls)):
        with SessionLocal() as db:
            again=pay.create_offtaker_payment(db,tenant=db.get(Tenant,t.id),sub=db.get(BillingReportSubscription,sid),match=_FakeMatch(amount=100),force=True)
    assert again["payment_id"]==res["payment_id"]
    assert len(calls)==1


def test_paid_event_cannot_reverse_refund(monkeypatch):
    calls=[]
    t,sid,res=_mint(monkeypatch,calls)
    with SessionLocal() as db:
        row=db.get(OfftakerPayment,res["payment_id"])
        row.status="refunded"
        db.commit()
        pay.mark_payment_paid(db,session_dict={"id":"cs_test_1","payment_status":"paid","metadata":{"kind":"offtaker_invoice","payment_id":str(row.id)}})
        assert row.status=="refunded"


def test_missing_payment_status_is_not_proof_of_payment(monkeypatch):
    calls=[]
    t,sid,res=_mint(monkeypatch,calls)
    with SessionLocal() as db:
        row=db.get(OfftakerPayment,res["payment_id"])
        pay.mark_payment_paid(db,session_dict={"id":"cs_test_1","metadata":{"kind":"offtaker_invoice","payment_id":str(row.id)}})
        assert row.status=="open"


def test_paid_metadata_cannot_select_different_checkout(monkeypatch):
    calls=[]
    t,sid,res=_mint(monkeypatch,calls)
    with SessionLocal() as db:
        row=db.get(OfftakerPayment,res["payment_id"])
        pay.mark_payment_paid(db,session_dict={"id":"cs_different","payment_status":"paid","metadata":{"kind":"offtaker_invoice","payment_id":str(row.id)}})
        assert row.status=="open"


def test_bulk_commit_detects_changed_rate_instead_of_identical_skip(client):
    from tests.test_offtaker_upload import _make_tenant, _make_array_with_bill
    tid,auth=_make_tenant()
    aid,ua=_make_array_with_bill(tid,"Rate Audit","GMP-audit",with_bill=True)
    row={"offtaker_name":"Rate Test", "array_id":aid,"utility_account_id":ua,"allocation_pct":0.1,"email":"rate@example.test","net_rate_per_kwh":0.20}
    headers={"Authorization":auth}
    body={"rows":[row],"cadence":"monthly","delivery_mode":"approval"}
    assert client.post("/v1/array-operator/billing/subscriptions/bulk-commit",json=body,headers=headers).json()["created"]==1
    row["net_rate_per_kwh"]=0.25
    result=client.post("/v1/array-operator/billing/subscriptions/bulk-commit",json=body,headers=headers).json()
    assert len(result["failed"])==1, result
    assert not result["skipped"]


def test_parallel_mail_receipts_are_not_cross_assigned(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import resend
    from api import notify, email_archive
    barrier=Barrier(2)
    monkeypatch.delenv("EMAIL_DRY_RUN",raising=False)
    monkeypatch.setattr(notify,"RESEND_API_KEY","re_test_dummy")
    monkeypatch.setattr(resend.Emails,"send",lambda params:{"id":"receipt-"+params["subject"]})
    # Hold both callers after provider acceptance and before their receipt read.
    monkeypatch.setattr(email_archive,"record",lambda **kw:barrier.wait(timeout=10))
    def send(label):
        assert notify._send_via_resend(to="operator@example.test",subject=label,html="test")
        return notify.last_resend_id()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(send,"A");second=pool.submit(send,"B")
        assert [first.result(),second.result()]==["receipt-A","receipt-B"]


def test_delayed_ach_failure_cannot_reopen_refunded_invoice(monkeypatch):
    calls=[]
    t,sid,res=_mint(monkeypatch,calls)
    with SessionLocal() as db:
        row=db.get(OfftakerPayment,res["payment_id"])
        row.status="refunded"
        row.stripe_checkout_session_id="cs_ach_"+str(row.id)
        db.commit()
        result=pay.mark_payment_async_failed(db,session_dict={"id":row.stripe_checkout_session_id,"metadata":{"kind":"offtaker_invoice"}})
        assert row.status=="refunded",result


def test_trueup_test_copy_cannot_mint_payable_invoice(monkeypatch):
    from types import SimpleNamespace
    from datetime import date
    from api.billing import trueup
    t=_tenant();sid=_sub(t.id,annual_trueup=True)
    settlement=SimpleNamespace(ok=True,window_end=date(2026,8,31),credit_usd=0,charge_usd=100,to_dict=lambda:{})
    monkeypatch.setattr(trueup,"compute_annual_trueup",lambda *a,**kw:settlement)
    monkeypatch.setattr(trueup,"build_trueup_match",lambda *a,**kw:_FakeMatch(amount=100))
    monkeypatch.setattr(delivery,"generate_files",lambda *a,**kw:[])
    monkeypatch.setattr("api.notify._send_via_resend",lambda **kw:True)
    mint=MagicMock(return_value={"ok":True,"pay_url":"https://unsafe.example/pay"})
    monkeypatch.setattr(pay,"create_offtaker_payment",mint)
    with SessionLocal() as db:
        result=delivery.deliver_trueup_subscription(db,db.get(BillingReportSubscription,sid),db.get(Tenant,t.id),is_test=True)
    assert result["ok"],result
    mint.assert_not_called()


def test_archive_alert_does_not_replace_invoice_receipt(monkeypatch):
    import resend
    from api import notify,email_archive
    monkeypatch.delenv("EMAIL_DRY_RUN",raising=False)
    monkeypatch.setattr(notify,"RESEND_API_KEY","re_test_dummy")
    monkeypatch.setattr(resend.Emails,"send",lambda params:{"id":"receipt-"+params["subject"]})
    def archive(**kwargs):
        if kwargs["subject"]=="invoice":
            notify._send_via_resend(to="operator@example.test",subject="alert",html="alert")
    monkeypatch.setattr(email_archive,"record",archive)
    assert notify._send_via_resend(to="operator@example.test",subject="invoice",html="invoice")
    assert notify.last_resend_id()=="receipt-invoice"
