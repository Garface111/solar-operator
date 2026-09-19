import base64
from datetime import date
from sqlalchemy import select
from api.db import SessionLocal
from api.models import Tenant, BillingReportSubscription, OfftakerInvoice
from api.billing import delivery, payments
from tests.test_billing_delivery import _make_tenant, _upload


def test_payment_hold_freezes_files_and_retries_without_live_workbook(client, monkeypatch):
    tid, auth = _make_tenant()
    sid = _upload(client, auth, "norwich.xlsx", send_mode="to_client",
                  client_email="original@example.test").json()["subscription"]["id"]
    rendered = []
    source = {"bytes": b"original invoice and utility evidence"}
    def render(match, formats, summary, directory, **kwargs):
        path = directory / "invoice.pdf"; path.write_bytes(source["bytes"])
        rendered.append(match.computed_invoice["amount_owed"])
        return [path]
    monkeypatch.setattr(delivery, "generate_files", render)
    monkeypatch.setattr(payments, "refresh_connect_status", lambda *a: {})
    monkeypatch.setattr(payments, "link_existing_connect_account", lambda *a: {})
    monkeypatch.setattr(payments, "create_offtaker_payment", lambda *a, **kw: {"ok": False, "error": "provider unavailable"})
    sent = []
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: sent.append(kw) or True)
    with SessionLocal() as db:
        sub, tenant = db.get(BillingReportSubscription, sid), db.get(Tenant, tid)
        tenant.offtaker_payment_policy = "online_required"; db.commit()
        held = delivery.deliver_subscription(db, sub, tenant)
        assert held["held"] and not sent
        invoice = db.scalar(select(OfftakerInvoice).where(OfftakerInvoice.subscription_id == sid))
        key, original_amount = invoice.period_key, invoice.amount_cents
        assert invoice.render_snapshot and len(rendered) == 1
        sub.source_workbook = b"corrupted replacement workbook"
        sub.client_email = "changed@example.test"
        tenant.offtaker_payment_policy = "offline"
        db.commit()
        source["bytes"] = b"corrected utility / edited template"
        result = delivery.deliver_subscription(db, sub, tenant, period_label=key)
        assert result["ok"], result
        assert result["amount_owed"] == original_amount / 100
    assert len(rendered) == 1 and len(sent) == 1
    assert sent[0]["to"] == "original@example.test"
    assert base64.b64decode(sent[0]["attachments"][0]["content"]) == b"original invoice and utility evidence"


def test_render_failure_rolls_back_credit_and_invoice_reservation(client, monkeypatch):
    tid, auth = _make_tenant()
    sid = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    def broken(*a, **kw): raise RuntimeError("renderer unavailable")
    monkeypatch.setattr(delivery, "generate_files", broken)
    with SessionLocal() as db:
        sub, tenant = db.get(BillingReportSubscription, sid), db.get(Tenant, tid)
        sub.pending_credit_usd = 500; sub.invoice_number_next = 42
        db.commit()
        result = delivery.deliver_subscription(db, sub, tenant)
        assert result["held"] and "renderer unavailable" in result["error"]
        db.refresh(sub)
        assert sub.pending_credit_usd == 500 and sub.invoice_number_next == 42
        assert db.scalar(select(OfftakerInvoice).where(OfftakerInvoice.subscription_id == sid)) is None


def test_trueup_retry_uses_frozen_settlement_without_recalculation(client, monkeypatch):
    from api.billing.trueup import TrueupSettlement
    tid, auth = _make_tenant()
    sid = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    settlement = TrueupSettlement(ok=True, window_start=date(2025,9,1),
        window_end=date(2026,8,31), total_budgeted=400,total_actual=500,
        difference=100, charge_usd=100)
    monkeypatch.setattr("api.billing.trueup.compute_annual_trueup", lambda *a, **kw: settlement)
    monkeypatch.setattr(delivery, "generate_files", lambda *a, **kw: [])
    monkeypatch.setattr(payments, "create_offtaker_payment", lambda *a, **kw: {"ok":False,"error":"unavailable"})
    sent=[]
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: sent.append(kw) or True)
    with SessionLocal() as db:
        sub, tenant = db.get(BillingReportSubscription,sid),db.get(Tenant,tid)
        sub.annual_trueup=True;tenant.offtaker_payment_policy="online_required";db.commit()
        assert delivery.deliver_trueup_subscription(db,sub,tenant,as_of=date(2026,9,1))["held"]
        def forbidden(*a,**kw): raise AssertionError("must not reread live annual evidence")
        monkeypatch.setattr("api.billing.trueup.compute_annual_trueup",forbidden)
        tenant.offtaker_payment_policy="offline";db.commit()
        result=delivery.deliver_trueup_subscription(db,sub,tenant,as_of=date(2026,9,1))
        assert result["ok"] and result["amount_owed"]==100
    assert len(sent)==1
