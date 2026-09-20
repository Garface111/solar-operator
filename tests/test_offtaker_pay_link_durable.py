"""Durable offtaker pay links (Sep 2026).

Stripe caps a Checkout Session at 24h; the invoice says "due within 28 days".
The invoice now carries /v1/array-operator/billing/pay/{token}; the click
mints or refreshes the Session. Stripe is mocked throughout.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace
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
    with patch("api.billing.payments.stripe.checkout.Session.retrieve", return_value={"status": "expired"}), patch("api.billing.payments.stripe.checkout.Session.create",
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
    with patch("api.billing.payments.stripe.checkout.Session.retrieve", return_value={"status": "expired"}), patch("api.billing.payments.stripe.checkout.Session.create",
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
    with patch("api.billing.payments.stripe.checkout.Session.retrieve", return_value={"status": "expired"}), patch("api.billing.payments.stripe.checkout.Session.create",
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
    expire = MagicMock(return_value={"status": "expired"})
    with patch("api.billing.payments.stripe.checkout.Session.retrieve", return_value={"status": "expired"}), patch("api.billing.payments.stripe.checkout.Session.expire", expire), \
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
    expire = MagicMock(return_value={"status": "expired"})
    with patch("api.billing.payments.stripe.checkout.Session.retrieve",
               return_value=_Sess(id="cs_test_1", status="open", payment_status="unpaid")), \
            patch("api.billing.payments.stripe.checkout.Session.expire", expire), \
\
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
    retrieve.assert_called_once()
    assert retrieve.call_args.args[0] == "cs_test_1"
    assert retrieve.call_args.kwargs["stripe_account"] == t.stripe_connect_account_id
    assert retrieve.call_args.kwargs.get("api_key")            # keyed to the owning platform


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


def test_pinned_methods_fall_back_to_automatic_when_the_account_rejects_them(monkeypatch):
    """ACH pinned by env but the connected account's ACH capability is not
    active yet: Stripe rejects the pin, we retry without it — the offtaker
    still gets a working Pay button instead of no button at all."""
    import stripe as _stripe
    monkeypatch.setenv("AO_OFFTAKER_PAYMENT_METHODS", "us_bank_account,card")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    t = _tenant()
    sid = _sub(t.id)
    calls: list = []

    def create(**kwargs):
        calls.append(kwargs)
        if "payment_method_types" in kwargs:
            raise _stripe.error.InvalidRequestError(
                "The payment method type provided: us_bank_account is invalid.",
                "payment_method_types")
        return _Sess(id="cs_fb_1", url="https://checkout.stripe.com/c/pay/cs_fb_1",
                     payment_intent=None)

    with patch("api.billing.payments.stripe.checkout.Session.create", side_effect=create):
        with SessionLocal() as db:
            res = pay.create_offtaker_payment(
                db, tenant=db.get(Tenant, t.id),
                sub=db.get(BillingReportSubscription, sid), match=_FakeMatch(amount=100.0))
    assert res["ok"], res
    assert len(calls) == 2
    assert calls[0]["payment_method_types"] == ["us_bank_account", "card"]
    assert "payment_method_types" not in calls[1]


# ─── platform account: Energy Agent for new connections, legacy stays put ───

def test_platform_routing_and_keys(monkeypatch):
    monkeypatch.setattr(pay, "_same_platform_account", lambda: False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_solar")
    monkeypatch.delenv("STRIPE_AO_SECRET_KEY", raising=False)
    fresh = SimpleNamespace(stripe_connect_account_id=None, stripe_connect_platform=None)
    legacy = SimpleNamespace(stripe_connect_account_id="acct_old", stripe_connect_platform=None)
    assert pay.platform_for(fresh) == "solar_operator"            # no AO key yet → unchanged
    monkeypatch.setenv("STRIPE_AO_SECRET_KEY", "sk_test_agent")
    assert pay.platform_for(fresh) == "energy_agent"
    assert pay.platform_for(legacy) == "solar_operator"           # connected before the split
    tagged = SimpleNamespace(stripe_connect_account_id="acct_new", stripe_connect_platform="energy_agent")
    assert pay.platform_for(tagged) == "energy_agent"
    assert pay._api_kw(tagged) == {"api_key": "sk_test_agent"}
    assert pay._api_kw(legacy) == {"api_key": "sk_test_solar"}
    row_ea = SimpleNamespace(stripe_account_id="acct_new", stripe_platform="energy_agent")
    row_old = SimpleNamespace(stripe_account_id="acct_old", stripe_platform=None)
    assert pay._stripe_kw(row_ea) == {"stripe_account": "acct_new", "api_key": "sk_test_agent"}
    assert pay._stripe_kw(row_old) == {"stripe_account": "acct_old", "api_key": "sk_test_solar"}
    assert pay.platform_label("energy_agent") == "Energy Agent"
    assert pay.stripe_fee_copy() == {"ach": "0.8%, capped at $5", "card": "2.9% + 30¢"}


def test_mint_uses_the_tenants_platform_key_and_stamps_the_row(monkeypatch):
    monkeypatch.setattr(pay, "_same_platform_account", lambda: False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_solar")
    monkeypatch.setenv("STRIPE_AO_SECRET_KEY", "sk_test_agent")
    calls: list = []
    t = _tenant(stripe_connect_platform="energy_agent")
    sid = _sub(t.id)
    with patch("api.billing.payments.stripe.checkout.Session.create",
               side_effect=_fake_create(calls)):
        with SessionLocal() as db:
            res = pay.create_offtaker_payment(
                db, tenant=db.get(Tenant, t.id),
                sub=db.get(BillingReportSubscription, sid), match=_FakeMatch(amount=100.0))
    assert res["ok"]
    assert calls[0]["api_key"] == "sk_test_agent"
    with SessionLocal() as db:
        assert db.get(OfftakerPayment, res["payment_id"]).stripe_platform == "energy_agent"
    calls2: list = []
    t2 = _tenant()   # account id set, platform NULL → legacy
    sid2 = _sub(t2.id)
    with patch("api.billing.payments.stripe.checkout.Session.create",
               side_effect=_fake_create(calls2)):
        with SessionLocal() as db:
            pay.create_offtaker_payment(db, tenant=db.get(Tenant, t2.id),
                                        sub=db.get(BillingReportSubscription, sid2),
                                        match=_FakeMatch(amount=100.0))
    assert calls2[0]["api_key"] == "sk_test_solar"


def test_connect_status_reports_fee_split_and_platform(client, monkeypatch):
    monkeypatch.setattr(pay, "_same_platform_account", lambda: False)
    monkeypatch.setenv("STRIPE_AO_SECRET_KEY", "sk_test_agent")
    from api.account import mint_session_for_tenant
    t = _tenant(stripe_connect_account_id=None, stripe_connect_charges_enabled=False)
    with patch("api.billing.payments.refresh_connect_status", return_value={"ok": True, "connected": False}):
        r = client.get("/v1/array-operator/billing/payments/connect",
                       headers={"Authorization": f"Bearer {mint_session_for_tenant(t.id)}"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["stripe_fees"]["ach"].startswith("0.8%") and "2.9%" in b["stripe_fees"]["card"]
    assert b["platform"] == "energy_agent" and b["platform_name"] == "Energy Agent"


def test_webhook_verifier_tries_the_energy_agent_secrets(monkeypatch):
    from api import stripe_webhook as wh
    monkeypatch.setattr(wh, "STRIPE_WEBHOOK_SECRET", "whsec_solar")
    monkeypatch.setattr(wh, "STRIPE_CONNECT_WEBHOOK_SECRET", "")
    monkeypatch.setattr(wh, "STRIPE_AO_WEBHOOK_SECRET", "whsec_agent")
    monkeypatch.setattr(wh, "STRIPE_AO_CONNECT_WEBHOOK_SECRET", "whsec_agent_connect")
    seen = []

    def fake_construct(payload, sig, secret):
        seen.append(secret)
        if secret != "whsec_agent_connect":
            raise wh.stripe.error.SignatureVerificationError("no", sig or "")
        return {"id": "evt_1", "type": "x"}

    with patch("api.stripe_webhook.stripe.Webhook.construct_event", side_effect=fake_construct):
        ev = wh._construct_signed_event(b"{}", "t=1,v1=abc")
    assert ev["id"] == "evt_1"
    assert seen == ["whsec_solar", "whsec_agent", "whsec_agent_connect"]


def test_unfinished_solar_operator_onboarding_restarts_on_energy_agent(monkeypatch):
    monkeypatch.setattr(pay, "_same_platform_account", lambda: False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_solar")
    monkeypatch.setenv("STRIPE_AO_SECRET_KEY", "sk_test_agent")
    t = _tenant(stripe_connect_account_id="acct_old", stripe_connect_charges_enabled=False,
                stripe_connect_platform=None)
    created = {}

    def fake_retrieve(acct_id, **kw):
        return {"id": acct_id, "charges_enabled": False, "details_submitted": False}

    def fake_create(**kw):
        created.update(kw)
        return {"id": "acct_new"}

    with patch("api.billing.payments.stripe.Account.retrieve", side_effect=fake_retrieve),             patch("api.billing.payments.stripe.Account.create", side_effect=fake_create):
        with SessionLocal() as db:
            res = pay.create_or_get_connect_account(db, db.get(Tenant, t.id))
    assert res["ok"] and res["account_id"] == "acct_new"
    assert created["api_key"] == "sk_test_agent"
    with SessionLocal() as db:
        row = db.get(Tenant, t.id)
        assert row.stripe_connect_account_id == "acct_new"
        assert row.stripe_connect_platform == "energy_agent"
    # A FINISHED legacy account is left where it is.
    t2 = _tenant(stripe_connect_account_id="acct_done", stripe_connect_charges_enabled=True,
                 stripe_connect_platform=None)
    with patch("api.billing.payments.stripe.Account.retrieve",
               return_value={"id": "acct_done", "charges_enabled": True, "details_submitted": True}),             patch("api.billing.payments.stripe.Account.create", side_effect=AssertionError("must not create")):
        with SessionLocal() as db:
            res2 = pay.create_or_get_connect_account(db, db.get(Tenant, t2.id))
    assert res2["account_id"] == "acct_done"


def test_two_keys_to_one_stripe_account_are_one_platform(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_a")
    monkeypatch.setenv("STRIPE_AO_SECRET_KEY", "sk_test_b")
    pay._SAME_ACCOUNT.clear()
    with patch("api.billing.payments.stripe.Account.retrieve", return_value={"id": "acct_same"}):
        assert pay._same_platform_account() is True
        fresh = SimpleNamespace(stripe_connect_account_id=None, stripe_connect_platform=None)
        assert pay.platform_for(fresh) == "solar_operator"       # one platform, no split
        # and an unfinished onboarding is NOT abandoned
        t = _tenant(stripe_connect_account_id="acct_half", stripe_connect_charges_enabled=False,
                    stripe_connect_platform=None)
    with patch("api.billing.payments.stripe.Account.retrieve",
               return_value={"id": "acct_half", "charges_enabled": False, "details_submitted": False}),             patch("api.billing.payments.stripe.Account.create", side_effect=AssertionError("must not create")):
        with SessionLocal() as db:
            res = pay.create_or_get_connect_account(db, db.get(Tenant, t.id))
    assert res["account_id"] == "acct_half"
    pay._SAME_ACCOUNT.clear()
