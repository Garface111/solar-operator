"""Durable offtaker pay links (Sep 2026).

Stripe caps a Checkout Session at 24h; the invoice says "due within 28 days".
The invoice now carries /v1/array-operator/billing/pay/{token}; the click
mints or refreshes the Session. Stripe is mocked throughout.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from api.billing import payments as pay
from api.db import SessionLocal
from api.models import BillingReportSubscription, OfftakerPayment, Tenant

from tests.test_offtaker_payments import _FakeMatch, _sub, _tenant

_PAY = "/v1/array-operator/billing/pay/"


class _Sess(dict):
    pass


def _fake_create(calls: list, prefix: str = "cs_test_"):
    def create(**kwargs):
        calls.append(kwargs)
        n = f"{prefix}{len(calls)}"
        return _Sess(id=n, url=f"https://checkout.stripe.com/c/pay/{n}", payment_intent=None)
    return create


def _mint(monkeypatch, calls, amount=100.0):
    t = _tenant()
    sid = _sub(t.id)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    with patch("api.billing.payments.stripe.checkout.Session.create",
               side_effect=_fake_create(calls)):
        with SessionLocal() as db:
            res = pay.create_offtaker_payment(
                db, tenant=db.get(Tenant, t.id),
                sub=db.get(BillingReportSubscription, sid),
                match=_FakeMatch(amount=amount))
    assert res["ok"], res
    return t, sid, res


# ─── minting ────────────────────────────────────────────────────────────────

def test_create_mints_durable_url_token_and_expiry(monkeypatch):
    calls: list = []
    t, sid, res = _mint(monkeypatch, calls)
    assert _PAY in res["pay_url"]
    assert res["checkout_url"].startswith("https://checkout.stripe.com/")
    assert calls[0]["expires_at"] - __import__("time").time() <= 23 * 3600
    with SessionLocal() as db:
        row = db.get(OfftakerPayment, res["payment_id"])
        assert row.pay_token and len(row.pay_token) >= 24
        assert row.pay_url.endswith(_PAY + row.pay_token)
        assert row.stripe_checkout_session_id == "cs_test_1"
        left = (row.checkout_expires_at - datetime.utcnow()).total_seconds()
        assert 22 * 3600 < left <= 23 * 3600


def test_resend_reuses_the_same_durable_link_even_after_expiry(monkeypatch):
    calls: list = []
    t, sid, r1 = _mint(monkeypatch, calls)
    with SessionLocal() as db:
        db.get(OfftakerPayment, r1["payment_id"]).status = "expired"
        db.commit()
    with patch("api.billing.payments.stripe.checkout.Session.create",
               side_effect=_fake_create(calls)):
        with SessionLocal() as db:
            r2 = pay.create_offtaker_payment(
                db, tenant=db.get(Tenant, t.id),
                sub=db.get(BillingReportSubscription, sid), match=_FakeMatch(amount=100.0))
    assert r2["reused"] is True
    assert r2["payment_id"] == r1["payment_id"]
    assert r2["pay_url"] == r1["pay_url"]
    assert len(calls) == 1                      # no second Stripe mint at send time


def test_legacy_open_row_without_token_is_never_reused(monkeypatch):
    t = _tenant()
    sid = _sub(t.id)
    with SessionLocal() as db:
        db.add(OfftakerPayment(
            tenant_id=t.id, subscription_id=sid, invoice_number="2026-06",
            period_key="2026-06-30", amount_cents=10_000, fee_cents=50,
            status="open", stripe_checkout_session_id="cs_legacy",
            pay_url="https://checkout.stripe.com/c/pay/cs_legacy", pay_token=None))
        db.commit()
    calls: list = []
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    with patch("api.billing.payments.stripe.checkout.Session.create",
               side_effect=_fake_create(calls)):
        with SessionLocal() as db:
            res = pay.create_offtaker_payment(
                db, tenant=db.get(Tenant, t.id),
                sub=db.get(BillingReportSubscription, sid), match=_FakeMatch(amount=100.0))
    assert res["ok"] and not res.get("reused")
    assert _PAY in res["pay_url"]              # a dead Stripe url is never re-sent
    assert len(calls) == 1


# ─── the click ──────────────────────────────────────────────────────────────

def test_click_on_fresh_session_redirects_without_reminting(client, monkeypatch):
    calls: list = []
    t, sid, res = _mint(monkeypatch, calls)
    token = res["pay_url"].rsplit("/", 1)[-1]
    retrieve = MagicMock(return_value=_Sess(
        id="cs_test_1", status="open", payment_status="unpaid",
        url="https://checkout.stripe.com/c/pay/cs_test_1"))
    with patch("api.billing.payments.stripe.checkout.Session.retrieve", retrieve), \
            patch("api.billing.payments.stripe.checkout.Session.create",
                  side_effect=_fake_create(calls)):
        r = client.get(_PAY + token, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    assert r.headers.get("cache-control") == "no-store"
    assert len(calls) == 1                      # reused, not re-minted


def test_click_after_stripe_window_lapsed_remints_on_the_same_row(client, monkeypatch):
    calls: list = []
    t, sid, res = _mint(monkeypatch, calls)
    token = res["pay_url"].rsplit("/", 1)[-1]
    with SessionLocal() as db:
        row = db.get(OfftakerPayment, res["payment_id"])
        row.checkout_expires_at = datetime.utcnow() - timedelta(hours=1)   # day three
        row.status = "expired"
        db.commit()
    expire = MagicMock()
    with patch("api.billing.payments.stripe.checkout.Session.expire", expire), \
            patch("api.billing.payments.stripe.checkout.Session.create",
                  side_effect=_fake_create(calls)):
        r = client.get(_PAY + token, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_2"
    assert len(calls) == 2
    with SessionLocal() as db:
        row = db.get(OfftakerPayment, res["payment_id"])
        assert row.stripe_checkout_session_id == "cs_test_2"
        assert row.status == "open"
        assert row.pay_token == token           # the link on the invoice is unchanged
        assert (row.checkout_expires_at - datetime.utcnow()).total_seconds() > 22 * 3600


def test_click_with_stale_open_session_expires_the_old_one_first(client, monkeypatch):
    calls: list = []
    t, sid, res = _mint(monkeypatch, calls)
    token = res["pay_url"].rsplit("/", 1)[-1]
    with SessionLocal() as db:
        db.get(OfftakerPayment, res["payment_id"]).checkout_expires_at = (
            datetime.utcnow() + timedelta(minutes=5))   # inside the refresh grace
        db.commit()
    expire = MagicMock()
    with patch("api.billing.payments.stripe.checkout.Session.expire", expire), \
            patch("api.billing.payments.stripe.checkout.Session.create",
                  side_effect=_fake_create(calls)):
        r = client.get(_PAY + token, follow_redirects=False)
    assert r.status_code == 303
    expire.assert_called_once()                    # never two live links per invoice
    assert expire.call_args.args[0] == "cs_test_1"
    assert expire.call_args.kwargs.get("stripe_account") == t.stripe_connect_account_id


def test_click_on_paid_row_shows_receipt_page_not_stripe(client):
    t = _tenant()
    sid = _sub(t.id)
    token = secrets.token_urlsafe(24)
    with SessionLocal() as db:
        db.add(OfftakerPayment(
            tenant_id=t.id, subscription_id=sid, invoice_number="INV-77",
            period_key="2026-06-30", amount_cents=12_345, fee_cents=61,
            status="paid", paid_at=datetime(2026, 9, 3, 14, 0), pay_token=token,
            pay_url="x", customer_name="Town of Test"))
        db.commit()
    r = client.get(_PAY + token, follow_redirects=False)
    assert r.status_code == 200
    assert "already been paid" in r.text
    assert "$123.45" in r.text and "INV-77" in r.text


def test_click_when_owner_has_not_finished_connect_is_honest(client):
    t = _tenant(stripe_connect_account_id=None, stripe_connect_charges_enabled=False)
    sid = _sub(t.id)
    token = secrets.token_urlsafe(24)
    with SessionLocal() as db:
        db.add(OfftakerPayment(
            tenant_id=t.id, subscription_id=sid, invoice_number="INV-1",
            period_key="2026-06-30", amount_cents=5_000, fee_cents=25,
            status="open", pay_token=token, pay_url="x"))
        db.commit()
    with patch("api.billing.payments.create_or_get_connect_account", return_value={}), \
            patch("api.billing.payments.link_existing_connect_account", return_value={}):
        r = client.get(_PAY + token, follow_redirects=False)
    assert r.status_code == 409
    assert "setting up online payments" in r.text


def test_unknown_token_is_404_page(client):
    r = client.get(_PAY + "not-a-real-token", follow_redirects=False)
    assert r.status_code == 404
    assert "couldn" in r.text.lower()


# ─── lifecycle webhooks ─────────────────────────────────────────────────────

def _row(t, sid, **kw):
    defaults = dict(tenant_id=t.id, subscription_id=sid, invoice_number="INV-9",
                    period_key="2026-06-30", amount_cents=10_000, fee_cents=50,
                    status="open", pay_token=secrets.token_urlsafe(24), pay_url="x")
    defaults.update(kw)
    with SessionLocal() as db:
        row = OfftakerPayment(**defaults)
        db.add(row)
        db.commit()
        return row.id


def test_expired_event_only_flips_the_row_whose_session_it_is():
    t = _tenant()
    sid = _sub(t.id)
    rid = _row(t, sid, stripe_checkout_session_id="cs_old")
    meta = {"kind": "offtaker_invoice"}
    with SessionLocal() as db:
        res = pay.mark_payment_expired(db, session_dict={"id": "cs_old", "metadata": meta})
        assert res.get("expired") is True
        assert db.get(OfftakerPayment, rid).status == "expired"
        # Re-minted since → the old session's event must not touch the row.
        db.get(OfftakerPayment, rid).stripe_checkout_session_id = "cs_new"
        db.get(OfftakerPayment, rid).status = "open"
        db.commit()
        res2 = pay.mark_payment_expired(db, session_dict={"id": "cs_old", "metadata": meta})
        assert "ignored" in res2
        assert db.get(OfftakerPayment, rid).status == "open"


def test_async_failed_marks_unpaid_and_full_refund_flips_paid():
    t = _tenant()
    sid = _sub(t.id)
    rid = _row(t, sid, stripe_checkout_session_id="cs_ach")
    with SessionLocal() as db:
        res = pay.mark_payment_async_failed(
            db, session_dict={"id": "cs_ach", "metadata": {"kind": "offtaker_invoice"}})
        assert res.get("failed") is True
        assert db.get(OfftakerPayment, rid).status == "failed"
    rid2 = _row(t, sid, status="paid", stripe_payment_intent_id="pi_9",
                paid_at=datetime.utcnow(), period_key="2026-07-31")
    with SessionLocal() as db:
        part = pay.mark_payment_refunded(
            db, charge_dict={"payment_intent": "pi_9", "refunded": False,
                             "amount_refunded": 2_500})
        assert part["ok"] and part["refunded"] is False
        assert db.get(OfftakerPayment, rid2).status == "paid"
        full = pay.mark_payment_refunded(
            db, charge_dict={"payment_intent": "pi_9", "refunded": True,
                             "amount_refunded": 10_000})
        assert full["refunded"] is True
        assert db.get(OfftakerPayment, rid2).status == "refunded"


# ─── charge model: the OPERATOR pays Stripe's fee ───────────────────────────

def test_direct_charges_are_the_default_and_live_on_the_operator_account(monkeypatch):
    monkeypatch.delenv("AO_OFFTAKER_CHARGE_MODEL", raising=False)
    calls: list = []
    t, sid, res = _mint(monkeypatch, calls)
    kw = calls[0]
    assert kw["stripe_account"] == t.stripe_connect_account_id     # Stripe-Account header
    assert "transfer_data" not in kw["payment_intent_data"]        # no platform charge
    assert kw["payment_intent_data"]["application_fee_amount"] == 50
    assert "payment_method_types" not in kw                        # Dashboard decides (ACH on)
    with SessionLocal() as db:
        assert db.get(OfftakerPayment, res["payment_id"]).stripe_account_id == \
            t.stripe_connect_account_id


def test_destination_model_is_still_available_by_env_and_per_tenant(monkeypatch):
    monkeypatch.setenv("AO_OFFTAKER_CHARGE_MODEL", "destination")
    calls: list = []
    t, sid, res = _mint(monkeypatch, calls)
    kw = calls[0]
    assert "stripe_account" not in kw
    assert kw["payment_intent_data"]["transfer_data"]["destination"] == t.stripe_connect_account_id
    with SessionLocal() as db:
        assert db.get(OfftakerPayment, res["payment_id"]).stripe_account_id is None
    # A per-tenant override beats the env, and junk falls back to direct.
    t2 = _tenant(offtaker_charge_model="direct")
    t3 = _tenant(offtaker_charge_model="banana")
    monkeypatch.delenv("AO_OFFTAKER_CHARGE_MODEL", raising=False)
    with SessionLocal() as db:
        assert pay.charge_model_for(db.get(Tenant, t2.id)) == "direct"
        assert pay.charge_model_for(db.get(Tenant, t3.id)) == "direct"


def test_payment_method_pin_is_env_driven(monkeypatch):
    monkeypatch.setenv("AO_OFFTAKER_PAYMENT_METHODS", "us_bank_account, card")
    calls: list = []
    _mint(monkeypatch, calls)
    assert calls[0]["payment_method_types"] == ["us_bank_account", "card"]


def test_click_addresses_the_account_the_session_lives_on(client, monkeypatch):
    calls: list = []
    t, sid, res = _mint(monkeypatch, calls)
    token = res["pay_url"].rsplit("/", 1)[-1]
    retrieve = MagicMock(return_value=_Sess(
        id="cs_test_1", status="open", payment_status="unpaid",
        url="https://checkout.stripe.com/c/pay/cs_test_1"))
    with patch("api.billing.payments.stripe.checkout.Session.retrieve", retrieve):
        r = client.get(_PAY + token, follow_redirects=False)
    assert r.status_code == 303
    retrieve.assert_called_once_with("cs_test_1", stripe_account=t.stripe_connect_account_id)


def test_connect_account_requests_ach_and_webhook_accepts_connect_secret():
    src = open(pay.__file__, encoding="utf-8").read()
    assert '"us_bank_account_ach_payments": {"requested": True}' in src
    from api import stripe_webhook as wh
    assert hasattr(wh, "STRIPE_CONNECT_WEBHOOK_SECRET")
    assert callable(wh._construct_signed_event)


def test_webhook_dispatch_knows_the_lifecycle_events():
    from api import stripe_webhook as wh
    src = open(wh.__file__, encoding="utf-8").read()
    for ev in ("checkout.session.expired", "checkout.session.async_payment_succeeded",
               "checkout.session.async_payment_failed", "charge.refunded"):
        assert f'"{ev}":' in src
