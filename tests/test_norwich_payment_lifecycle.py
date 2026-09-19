"""Adversarial payment lifecycle tests. All external payment/email effects mocked."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import secrets

import pytest
from sqlalchemy import select, func
from api.db import SessionLocal
from api.models import (Tenant, BillingReportSubscription, OfftakerPayment,
    OfftakerRefund, OfftakerInvoice, OfftakerSettlement)
from api.billing import payments as pay
from api.billing.invoice_ledger import list_payment_rows
from tests.test_offtaker_payments import _tenant, _sub, _FakeMatch


@pytest.fixture
def book(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_mock")
    t = _tenant()
    sid = _sub(t.id)
    monkeypatch.setattr("api.billing.invoice_ledger.sync_payment_into_ledger", lambda *a: {})
    return t, sid


def create(book, amount=100):
    t, sid = book
    with SessionLocal() as db:
        return pay.create_offtaker_payment(db, tenant=db.get(Tenant, t.id),
            sub=db.get(BillingReportSubscription, sid), match=_FakeMatch(amount=amount))


def test_concurrent_first_mints_have_one_collectible_session(book):
    requests = {}
    def stripe_create(**kw):
        requests.setdefault(kw["idempotency_key"], dict(id="cs_"+secrets.token_hex(5),
            url="https://mock/checkout", payment_intent=None))
        return requests[kw["idempotency_key"]]
    with patch.object(pay.stripe.checkout.Session, "create", side_effect=stripe_create):
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: create(book), range(2)))
    assert all(r["ok"] for r in results)
    assert results[0]["payment_id"] == results[1]["payment_id"]
    assert len(requests) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(OfftakerPayment).where(
            OfftakerPayment.subscription_id == book[1])) == 1


def test_crash_after_provider_creation_replays_exact_request(book):
    requests = []
    def create_then_timeout(**kw):
        requests.append(kw)
        raise TimeoutError("Provider accepted, response lost")
    with patch.object(pay.stripe.checkout.Session, "create", side_effect=create_then_timeout):
        assert not create(book)["ok"]
    with SessionLocal() as db:
        row = db.scalar(select(OfftakerPayment).where(OfftakerPayment.subscription_id == book[1]))
        token = row.pay_token
        assert row.checkout_request == requests[0]
    def replay(**kw):
        requests.append(kw)
        return dict(id="cs_recovered", url="https://mock/recovered", payment_intent=None)
    with patch.object(pay.stripe.checkout.Session, "create", side_effect=replay):
        with SessionLocal() as db:
            result = pay.resolve_pay_link(db, token)
    assert result["action"] == "redirect"
    assert requests[0] == requests[1]


def test_unknown_creation_beyond_provider_retention_is_held(book):
    with patch.object(pay.stripe.checkout.Session, "create", side_effect=TimeoutError):
        create(book)
    with SessionLocal() as db:
        row = db.scalar(select(OfftakerPayment).where(OfftakerPayment.subscription_id == book[1]))
        row.checkout_requested_at = datetime.utcnow() - timedelta(days=2)
        token = row.pay_token
        db.commit()
    with patch.object(pay.stripe.checkout.Session, "create") as mint:
        with SessionLocal() as db:
            assert pay.resolve_pay_link(db, token)["action"] == "unavailable"
        mint.assert_not_called()


def test_revised_amount_retires_old_token_only_after_proven_expiry(book):
    with patch.object(pay.stripe.checkout.Session, "create",
        return_value=dict(id="cs_old_revision", url="https://mock/old", payment_intent=None)):
        first = create(book)
    token = first["pay_url"].rsplit("/", 1)[-1]
    with patch.object(pay.stripe.checkout.Session, "retrieve",
        return_value=dict(status="complete", payment_status="unpaid")), patch.object(
        pay.stripe.checkout.Session, "create") as mint:
        assert not create(book, 120)["ok"]
        mint.assert_not_called()
    with patch.object(pay.stripe.checkout.Session, "retrieve",
        return_value=dict(status="open", payment_status="unpaid")), patch.object(
        pay.stripe.checkout.Session, "expire", return_value={"status":"expired"}), patch.object(
        pay.stripe.checkout.Session, "create",
        return_value=dict(id="cs_new_revision", url="https://mock/new", payment_intent=None)):
        revised = create(book, 120)
    assert revised["ok"] and revised["payment_id"] != first["payment_id"]
    with SessionLocal() as db:
        assert pay.resolve_pay_link(db, token)["action"] == "not_found"
        assert db.get(OfftakerPayment, first["payment_id"]).status == "superseded"


def paid_row(book):
    t, sid = book
    session_id = "cs_" + secrets.token_hex(5)
    with SessionLocal() as db:
        row = OfftakerPayment(tenant_id=t.id, subscription_id=sid,
            period_key="2026-06-30", invoice_number="INV", amount_cents=10000,
            fee_cents=50, status="open", stripe_checkout_session_id=session_id,
            stripe_account_id=t.stripe_connect_account_id)
        db.add(row); db.commit()
        pid = row.id
    return pid, dict(id=session_id, payment_status="paid", amount_total=10000,
        currency="usd", _stripe_account=t.stripe_connect_account_id,
        payment_intent="pi_"+str(pid), metadata={"kind":"offtaker_invoice", "payment_id":str(pid)})


@pytest.mark.parametrize("field,value", [
    ("amount_total", 9999), ("currency", "eur"), ("_stripe_account", "acct_foreign"),
    ("currency", None), ("payment_status", "no_payment_required")])
def test_invalid_settlement_evidence_never_changes_balance(book, field, value):
    pid, event = paid_row(book); event[field] = value
    with SessionLocal() as db:
        result = pay.mark_payment_paid(db, session_dict=event)
        assert db.get(OfftakerPayment, pid).status == "open"
        assert not result.get("notify")


def test_concurrent_paid_events_and_receipt_retries_deduplicate(book):
    pid, event = paid_row(book)
    def settle(_):
        with SessionLocal() as db:
            return pay.mark_payment_paid(db, session_dict=event)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(settle, range(2)))
    assert sum(not r.get("duplicate", False) for r in results) == 1
    assert all(r["notify"] for r in results)
    with patch("api.notify._send_via_resend", return_value=True) as send:
        for result in results:
            assert pay.send_payment_received_emails(result["notify"])["sent"]
        assert send.call_count == 2  # one owner plus one offtaker, permanently keyed


def test_refunds_are_monotonic_deduplicated_and_reduce_net(book):
    pid, event = paid_row(book)
    with SessionLocal() as db:
        pay.mark_payment_paid(db, session_dict=event)
        charge = dict(payment_intent=event["payment_intent"], amount=10000, currency="usd",
            _stripe_account=event["_stripe_account"], amount_refunded=4000,
            refunds={"data":[{"id":"re_"+str(pid), "amount":4000,"status":"succeeded"}]})
        pay.mark_payment_refunded(db, charge_dict=charge)
        pay.mark_payment_refunded(db, charge_dict=charge)
        charge["amount_refunded"] = 2000
        pay.mark_payment_refunded(db, charge_dict=charge)
        assert db.get(OfftakerPayment, pid).refunded_cents == 4000
        assert db.scalar(select(func.count()).select_from(OfftakerRefund).where(
            OfftakerRefund.payment_id == pid)) == 1
        values = list_payment_rows(db, db.get(BillingReportSubscription, book[1]))
        assert values[0]["collected_usd"] == 59.50
        charge["amount_refunded"] = 10000
        pay.mark_payment_refunded(db, charge_dict=charge)
        pay.mark_payment_paid(db, session_dict=event)
        assert db.get(OfftakerPayment, pid).status == "refunded"


def test_offline_receipts_are_audited_idempotent_and_balance_limited(book):
    t, sid = book
    with SessionLocal() as db:
        invoice = OfftakerInvoice(tenant_id=t.id, subscription_id=sid,
            period_key="2026-06", amount_cents=10000, status="accepted", snapshot={})
        db.add(invoice); db.commit()
        invoice_id = invoice.id
        args = dict(tenant_id=t.id, invoice_id=invoice_id, amount_cents=4000,
            request_key="check-123", actor="owner", received_on=date(2026, 6, 30),
            note="Check 123 deposited", method="check")
        assert pay.record_offline_payment(db, **args)["outstanding_cents"] == 6000
        assert pay.record_offline_payment(db, **args)["duplicate"]
        with pytest.raises(ValueError, match="conflicts"):
            pay.record_offline_payment(db, **(args | {"amount_cents":5000}))
        with pytest.raises(ValueError, match="exceeds"):
            pay.record_offline_payment(db, **(args | {"request_key":"other", "amount_cents":7000}))
        values = list_payment_rows(db, db.get(BillingReportSubscription, sid))
        assert values[0]["collected_usd"] == 40
        with pytest.raises(ValueError, match="not found"):
            pay.record_offline_payment(db, **(args | {"tenant_id":"foreign"}))


def test_webhook_concurrent_duplicate_is_retryable_until_handler_commits(client, monkeypatch):
    import threading
    from api import stripe_webhook as webhook
    monkeypatch.setattr(webhook, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook, "_ON_RAILWAY", False)
    started, release = threading.Event(), threading.Event()
    event_id = "evt_" + secrets.token_hex(8)
    def handler(obj):
        started.set()
        assert release.wait(10)
        return {"ok": True}
    monkeypatch.setattr(webhook, "_process_connect_account_updated", handler)
    payload = {"id":event_id, "type":"account.updated", "data":{"object":{"id":"acct_mock"}}}
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.post, "/v1/stripe/webhook", json=payload)
        assert started.wait(5)
        second = client.post("/v1/stripe/webhook", json=payload)
        release.set()
        assert first.result().status_code == 200
    assert second.status_code == 503
    replay = client.post("/v1/stripe/webhook", json=payload)
    assert replay.json()["duplicate"]


def test_payment_receipt_provider_accept_then_process_crash_holds_replay(book):
    from api.models import BillingEmailDispatch
    pid, event = paid_row(book)
    with SessionLocal() as db:
        result = pay.mark_payment_paid(db, session_dict=event)
    # Model the exact durable state left by a process dying after handoff.
    with SessionLocal() as db:
        db.add(BillingEmailDispatch(tenant_id=book[0].id,
            key=f"payment:{pid}:receipt:offtaker", kind="payment_receipt",
            email={}, status="sending", attempts=1))
        db.commit()
    with patch("api.notify._send_via_resend", return_value=True) as send:
        retried = pay.send_payment_received_emails(result["notify"])
    assert not retried["offtaker"]
    assert retried["owner"]
    assert send.call_count == 1


def test_database_crash_after_checkout_creation_replays_same_key(book):
    calls = []
    def provider(**kw):
        calls.append(kw)
        return {"id":"cs_commit_crash","url":"https://mock/crash","payment_intent":None}
    with patch.object(pay.stripe.checkout.Session, "create", side_effect=provider):
        with SessionLocal() as db:
            original_commit = db.commit
            commits = 0
            def crashing_commit():
                nonlocal commits
                commits += 1
                if commits == 3:
                    raise SystemExit("process died after Stripe response")
                return original_commit()
            with patch.object(db, "commit", side_effect=crashing_commit):
                with pytest.raises(SystemExit):
                    pay.create_offtaker_payment(db, tenant=db.get(Tenant, book[0].id),
                        sub=db.get(BillingReportSubscription, book[1]), match=_FakeMatch(amount=100))
        with SessionLocal() as db:
            row = db.scalar(select(OfftakerPayment).where(OfftakerPayment.subscription_id==book[1]))
            assert row.stripe_checkout_session_id is None
            token = row.pay_token
        with SessionLocal() as db:
            assert pay.resolve_pay_link(db, token)["action"] == "redirect"
    assert len(calls) == 2 and calls[0] == calls[1]
