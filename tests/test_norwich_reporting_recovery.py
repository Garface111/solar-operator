"""Historical accounting and recovery regressions; no external effects."""
from datetime import date, datetime
from types import SimpleNamespace

from api.db import SessionLocal
from api.models import Tenant, BillingReportSubscription, OfftakerInvoice, OfftakerMonthlyReport, OfftakerPayment
from api.billing import monthly_report, trueup, delivery
from tests.test_offtaker_payments import _tenant, _sub, _FakeMatch


def _invoice(db, tid, sid, key, amount=10000, status="accepted", credit=0):
    inv = OfftakerInvoice(tenant_id=tid, subscription_id=sid, period_key=key,
        period_start=date.fromisoformat(key + "-01"), period_end=date.fromisoformat(key + "-28"),
        invoice_number=key, amount_cents=amount, credit_applied_cents=credit,
        customer_kwh=400, status=status, snapshot={"customer_name": "Original name"},
        sent_at=datetime(2026, 7, 1) if status == "accepted" else None,
        last_error="Missing settled utility bill" if status == "held" else None)
    db.add(inv)
    db.flush()
    return inv


def test_history_and_held_rows_survive_later_subscription_changes():
    t = _tenant(); sid = _sub(t.id); held = _sub(t.id)
    with SessionLocal() as db:
        _invoice(db, t.id, sid, "2026-06")
        _invoice(db, t.id, held, "2026-06", status="held")
        sub = db.get(BillingReportSubscription, sid)
        sub.customer_name = "Changed"; sub.last_sent_amount_usd = 999
        sub.last_sent_period_end = "2026-07-31"
        db.commit()
        rows = monthly_report.collect_rows(db, db.get(Tenant, t.id), "2026-06")
        assert len(rows) == 2
        issued = next(r for r in rows if r["invoice_status"] == "accepted")
        assert issued["offtaker"] == "Original name"
        assert issued["billed_usd"] == 100
        assert issued["generation_kwh"] == 400
        hold = next(r for r in rows if r["invoice_status"] == "held")
        assert hold["billed_usd"] is None
        assert hold["exception_reason"] == "Missing settled utility bill"
        assert monthly_report.summarize(rows)["exception_count"] == 1


def test_unsent_subscription_is_visible():
    t = _tenant(); _sub(t.id)
    with SessionLocal() as db:
        rows = monthly_report.collect_rows(db, db.get(Tenant, t.id), "2026-06")
        assert len(rows) == 1
        assert rows[0]["invoice_status"] == "unsent"
        assert rows[0]["exception_reason"]
        assert rows[0]["billed_usd"] is None


def test_old_report_gap_not_hidden_by_new_report():
    t = _tenant(); sid = _sub(t.id)
    with SessionLocal() as db:
        _invoice(db, t.id, sid, "2026-06")
        _invoice(db, t.id, sid, "2026-07")
        db.add(OfftakerMonthlyReport(tenant_id=t.id, period_key="2026-07", sent_at=datetime(2026,8,1)))
        db.commit()
        due = monthly_report.due_period(db, db.get(Tenant, t.id), now=datetime(2026,10,1))
        assert due["period_key"] == "2026-06"


def test_monthly_send_uses_one_stable_dispatch_identity_and_frozen_workbook(monkeypatch):
    from api.billing import dispatch
    t = _tenant(); sid = _sub(t.id); seen=[]
    def send(**kwargs):
        seen.append(kwargs)
        return {"ok": False, "uncertain": True, "error": "acceptance unknown"}
    monkeypatch.setattr(dispatch, "send_email_once", send)
    with SessionLocal() as db:
        _invoice(db,t.id,sid,"2026-06")
        db.commit()
        tenant=db.get(Tenant,t.id)
        monthly_report.send_report(db,tenant,"2026-06")
        db.get(BillingReportSubscription,sid).customer_name="Changed after first attempt"
        db.commit()
        monthly_report.send_report(db,tenant,"2026-06")
        assert seen[0]["key"] == seen[1]["key"] == "monthly-report:2026-06"
        assert seen[0]["email"]["attachments"] == seen[1]["email"]["attachments"]


def test_trueup_counts_issued_credits_once_and_keeps_unpaid_debt(monkeypatch):
    t = _tenant(); sid = _sub(t.id, budget_amount_usd=999)
    match = _FakeMatch(amount=999)
    match.computed_invoice.update(budget_override=True, solar_credit_value=50)
    monkeypatch.setattr(delivery, "build_match", lambda *a, **kw: match)
    with SessionLocal() as db:
        _invoice(db,t.id,sid,"2026-06",amount=8000,credit=2000)
        db.add(OfftakerPayment(tenant_id=t.id,subscription_id=sid,period_key="2026-06-28",invoice_number="2026-06",amount_cents=8000,fee_cents=0,status="pending"))
        db.commit()
        result=trueup.compute_annual_trueup(db.get(BillingReportSubscription,sid),as_of=date(2026,9,1))
        assert result.ok
        assert result.total_budgeted == 100
        assert result.charge_usd == 500
        row=db.query(OfftakerPayment).filter_by(subscription_id=sid).one()
        assert row.status == "pending" and row.amount_cents == 8000


def test_trueup_refuses_incomplete_actuals(monkeypatch):
    t = _tenant(); sid = _sub(t.id,budget_amount_usd=100)
    match=_FakeMatch(amount=100)
    match.computed_invoice.update(budget_override=True,solar_credit_value=50)
    def build(*a,**kw):
        return None if kw["period_label"] == "2026-06" else match
    monkeypatch.setattr(delivery,"build_match",build)
    with SessionLocal() as db:
        _invoice(db,t.id,sid,"2026-06")
        db.commit()
        result=trueup.compute_annual_trueup(db.get(BillingReportSubscription,sid),as_of=date(2026,9,1))
        assert not result.ok
        assert result.credit_usd == 0 and result.charge_usd == 0
        assert "Incomplete" in result.error


def test_trueup_refuses_uncertain_issued_history(monkeypatch):
    t = _tenant(); sid = _sub(t.id,budget_amount_usd=100)
    with SessionLocal() as db:
        _invoice(db,t.id,sid,"2026-06",status="uncertain")
        db.commit()
        result=trueup.compute_annual_trueup(db.get(BillingReportSubscription,sid),as_of=date(2026,9,1))
        assert not result.ok
        assert "Uncertain" in result.error


def test_concurrent_summary_calls_share_durable_claim(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    sent = []
    def mail(**kwargs):
        sent.append(kwargs); entered.set()
        assert release.wait(10)
        return True
    monkeypatch.setattr("api.notify._send_via_resend", mail)
    t=_tenant(); sid=_sub(t.id)
    with SessionLocal() as db:
        _invoice(db,t.id,sid,"2026-06"); db.commit()
    def run():
        with SessionLocal() as db:
            return monthly_report.send_report(db,db.get(Tenant,t.id),"2026-06")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(run)
        assert entered.wait(10)
        second=pool.submit(run)
        try:
            assert not second.result(timeout=10)["ok"]
        finally:
            release.set()
        assert first.result(timeout=10)["ok"]
    assert len(sent) == 1


def test_summary_commit_failure_after_acceptance_never_resends(monkeypatch):
    sent=[]
    monkeypatch.setattr("api.notify._send_via_resend",lambda **kw: sent.append(kw) or True)
    t=_tenant(); sid=_sub(t.id)
    with SessionLocal() as db:
        _invoice(db,t.id,sid,"2026-06"); db.commit()
        original=db.commit
        calls=[]
        def commit():
            calls.append(True)
            if len(calls)==2:
                raise RuntimeError("report stamp unavailable")
            return original()
        monkeypatch.setattr(db,"commit",commit)
        import pytest
        with pytest.raises(RuntimeError,match="stamp unavailable"):
            monthly_report.send_report(db,db.get(Tenant,t.id),"2026-06")
        db.rollback()
    with SessionLocal() as db:
        assert monthly_report.send_report(db,db.get(Tenant,t.id),"2026-06")["ok"]
    assert len(sent)==1


def test_offline_settlement_appears_in_monthly_cash_and_balance():
    from api.models import OfftakerSettlement
    t=_tenant();sid=_sub(t.id)
    with SessionLocal() as db:
        inv=_invoice(db,t.id,sid,"2026-06")
        db.add(OfftakerSettlement(tenant_id=t.id,invoice_id=inv.id,subscription_id=sid,
            amount_cents=6000,received_on=date(2026,7,2),request_key="offline-audit",
            actor="operator",method="check",note="Check receipt verified"))
        db.commit()
        row=monthly_report.collect_rows(db,db.get(Tenant,t.id),"2026-06")[0]
        assert row["collected_usd"] == 60
        assert row["gross_collected_usd"] == 60
        assert row["outstanding_usd"] == 40
        assert row["paid"] == "Partial payment"
