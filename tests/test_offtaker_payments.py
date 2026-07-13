"""V2 offtaker pay-links — fee math, Checkout create, webhook, delivery wiring.

Mocks Stripe so the suite never hits the network. Uses the real SQLite test DB
+ OfftakerPayment / Tenant Connect columns.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.billing import payments as pay
from api.db import SessionLocal
from api.models import (
    Tenant, BillingReportSubscription, OfftakerPayment, Array, DailyGeneration,
)


# ─── fee math ───────────────────────────────────────────────────────────────

def test_application_fee_default_0_5_percent():
    # $100.00 → 0.5% = $0.50
    assert pay.application_fee_cents(10_000, bps=50, min_cents=0) == 50
    # $5.19 → 0.5% = 2.595¢ → 2¢ (integer floor via //)
    assert pay.application_fee_cents(519, bps=50, min_cents=0) == 2
    # $1.00 → 0¢ at 0.5% (100 * 50 // 10000 = 0)
    assert pay.application_fee_cents(100, bps=50, min_cents=0) == 0
    # Default env bps is 50
    assert pay.DEFAULT_FEE_BPS == 50
    assert pay.application_fee_cents(10_000) == 50


def test_application_fee_never_eats_whole_amount():
    # Even at 100% bps we leave the connected account 1¢.
    assert pay.application_fee_cents(100, bps=10_000, min_cents=0) == 99
    assert pay.application_fee_cents(1, bps=10_000, min_cents=0) == 0
    assert pay.application_fee_cents(0, bps=150) == 0
    assert pay.application_fee_cents(-5, bps=150) == 0


def test_application_fee_min_floor():
    # 10¢ invoice at 0.5% = 0¢; min floor of 30¢ clamps — but still capped to amount-1.
    assert pay.application_fee_cents(10, bps=50, min_cents=30) == 9
    # $100 @ 0.5% = 50¢, above the 30¢ floor → 50
    assert pay.application_fee_cents(10_000, bps=50, min_cents=30) == 50


def test_dollars_to_cents():
    assert pay.dollars_to_cents(5.19) == 519
    assert pay.dollars_to_cents("12.34") == 1234
    assert pay.dollars_to_cents(0) == 0
    assert pay.dollars_to_cents(None) == 0
    assert pay.dollars_to_cents(-1) == 0


# ─── fixtures ───────────────────────────────────────────────────────────────

def _tenant(**kw) -> Tenant:
    tid = "ten_" + secrets.token_hex(5)
    # Unique Connect account id per tenant so parallel/prior tests don't
    # collide on the stripe_connect_account_id lookup.
    defaults = dict(
        id=tid, name="Pay Link Owner",
        contact_email=f"{tid}@owner.test",
        tenant_key="sol_live_" + secrets.token_urlsafe(10),
        plan="standard", active=True, product="array_operator",
        stripe_connect_account_id="acct_" + secrets.token_hex(6),
        stripe_connect_charges_enabled=True,
    )
    defaults.update(kw)
    with SessionLocal() as db:
        t = Tenant(**defaults)
        db.add(t)
        db.commit()
        db.refresh(t)
        # Snapshot attrs we need after the session closes (expire_on_commit).
        return SimpleNamespace(
            id=t.id,
            stripe_connect_account_id=t.stripe_connect_account_id,
            stripe_connect_charges_enabled=t.stripe_connect_charges_enabled,
        )


def _sub(tenant_id: str, **kw) -> BillingReportSubscription:
    defaults = dict(
        tenant_id=tenant_id,
        customer_name="Town of Test",
        client_email="offtaker@example.com",
        send_mode="to_client",
        allocation_pct=0.25,
        billing_model="percent_of_array",
        formats=["pdf"],
        enabled=True,
    )
    defaults.update(kw)
    with SessionLocal() as db:
        s = BillingReportSubscription(**defaults)
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    return sid  # type: ignore[return-value]


class _FakeMatch:
    """Minimal stand-in for BillingMatch used by create_offtaker_payment."""
    def __init__(self, amount=100.0, inv="2026-06", ps="2026-06-01", pe="2026-06-30"):
        self.matched = True
        self.latest_period = SimpleNamespace(end=date(2026, 6, 30))
        self.customer = {"name": "Town of Test", "email": "offtaker@example.com"}
        self.computed_invoice = {
            "amount_owed": amount,
            "invoice_number": inv,
            "period_start": ps,
            "period_end": pe,
            "kwh": 500,
        }


# ─── create_offtaker_payment ────────────────────────────────────────────────

def test_create_payment_skips_without_connect():
    t = _tenant(stripe_connect_account_id=None, stripe_connect_charges_enabled=False)
    sid = _sub(t.id)
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        tenant = db.get(Tenant, t.id)
        res = pay.create_offtaker_payment(db, tenant=tenant, sub=sub, match=_FakeMatch())
    assert res["ok"] is False
    assert res.get("skipped") is True
    assert "Connect" in (res.get("error") or "")


def test_create_payment_skips_tiny_amount():
    t = _tenant()
    sid = _sub(t.id)
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        tenant = db.get(Tenant, t.id)
        res = pay.create_offtaker_payment(
            db, tenant=tenant, sub=sub, match=_FakeMatch(amount=0.25))
    assert res["ok"] is False
    assert res.get("skipped")


def test_create_payment_mints_checkout_and_persists(monkeypatch):
    t = _tenant()
    sid = _sub(t.id)

    class _Sess(dict):
        pass

    def fake_create(**kwargs):
        # Assert destination charge shape.
        pi = kwargs["payment_intent_data"]
        assert pi["application_fee_amount"] == 50  # 0.5% of $100
        assert pi["transfer_data"]["destination"] == t.stripe_connect_account_id
        assert kwargs["metadata"]["kind"] == "offtaker_invoice"
        assert kwargs["mode"] == "payment"
        return _Sess(id="cs_test_123", url="https://checkout.stripe.com/c/pay/cs_test_123",
                     payment_intent="pi_test_123")

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("AO_OFFTAKER_FEE_BPS", "50")
    with patch("api.billing.payments.stripe.checkout.Session.create", side_effect=fake_create):
        with SessionLocal() as db:
            sub = db.get(BillingReportSubscription, sid)
            tenant = db.get(Tenant, t.id)
            res = pay.create_offtaker_payment(
                db, tenant=tenant, sub=sub, match=_FakeMatch(amount=100.0))

    assert res["ok"] is True
    assert res["pay_url"].startswith("https://checkout.stripe.com/")
    assert res["fee_cents"] == 50
    assert res["amount_cents"] == 10_000
    with SessionLocal() as db:
        row = db.get(OfftakerPayment, res["payment_id"])
        assert row is not None
        assert row.status == "open"
        assert row.stripe_checkout_session_id == "cs_test_123"
        assert row.fee_cents == 50
        assert row.period_key == "2026-06-30"


def test_create_payment_reuses_open_session(monkeypatch):
    t = _tenant()
    sid = _sub(t.id)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")

    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        return {"id": f"cs_{calls['n']}", "url": f"https://pay.test/{calls['n']}",
                "payment_intent": f"pi_{calls['n']}"}

    with patch("api.billing.payments.stripe.checkout.Session.create", side_effect=fake_create):
        with SessionLocal() as db:
            sub = db.get(BillingReportSubscription, sid)
            tenant = db.get(Tenant, t.id)
            r1 = pay.create_offtaker_payment(
                db, tenant=tenant, sub=sub, match=_FakeMatch())
            r2 = pay.create_offtaker_payment(
                db, tenant=tenant, sub=sub, match=_FakeMatch())
    assert r1["ok"] and r2["ok"]
    assert r1["payment_id"] == r2["payment_id"]
    assert r2.get("reused") is True
    assert calls["n"] == 1  # second call did not hit Stripe


# ─── webhook mark paid ──────────────────────────────────────────────────────

def test_mark_payment_paid_idempotent(monkeypatch):
    t = _tenant()
    sid = _sub(t.id)
    with SessionLocal() as db:
        row = OfftakerPayment(
            tenant_id=t.id, subscription_id=sid,
            invoice_number="2026-06", period_key="2026-06-30",
            amount_cents=10_000, fee_cents=50, status="open",
            stripe_checkout_session_id="cs_paid_1",
            pay_url="https://checkout.stripe.com/c/pay/cs_paid_1",
            customer_name="Town of Test",
        )
        db.add(row)
        db.commit()
        pid = row.id

    sess = {
        "id": "cs_paid_1",
        "payment_status": "paid",
        "payment_intent": "pi_paid_1",
        "amount_total": 10_000,
        "metadata": {
            "kind": "offtaker_invoice",
            "payment_id": str(pid),
            "tenant_id": t.id,
            "subscription_id": str(sid),
        },
    }
    with SessionLocal() as db:
        r1 = pay.mark_payment_paid(db, session_dict=sess)
        r2 = pay.mark_payment_paid(db, session_dict=sess)
    assert r1["ok"] and not r1.get("duplicate")
    assert r2["ok"] and r2.get("duplicate")
    with SessionLocal() as db:
        row = db.get(OfftakerPayment, pid)
        assert row.status == "paid"
        assert row.stripe_payment_intent_id == "pi_paid_1"
        assert row.paid_at is not None


def test_webhook_routes_offtaker_kind():
    """checkout.session.completed with kind=offtaker_invoice must NOT activate a tenant."""
    from api.stripe_webhook import _process_checkout_completed
    with patch("api.stripe_webhook._process_offtaker_invoice_paid") as h:
        h.return_value = {"ok": True, "payment_id": 1, "tenant": "ten_x"}
        out = _process_checkout_completed({
            "id": "cs_x",
            "metadata": {"kind": "offtaker_invoice", "payment_id": "1"},
            "payment_status": "paid",
        })
    assert out["ok"] is True
    h.assert_called_once()


def test_connect_account_updated_flips_flag():
    t = _tenant(stripe_connect_charges_enabled=False)
    with SessionLocal() as db:
        r = pay.sync_connect_from_account_event(db, {
            "id": t.stripe_connect_account_id,
            "charges_enabled": True,
            "details_submitted": True,
        })
    assert r["ok"] and r["changed"] and r["charges_enabled"]
    with SessionLocal() as db:
        tenant = db.get(Tenant, t.id)
        assert tenant.stripe_connect_charges_enabled is True


# ─── API endpoints ──────────────────────────────────────────────────────────

def test_payments_connect_status_endpoint(client):
    t = _tenant(stripe_connect_charges_enabled=True)
    auth = f"Bearer {mint_session_for_tenant(t.id)}"
    r = client.get("/v1/array-operator/billing/payments/connect",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["connected"] is True
    assert body["ready"] is True
    assert body["fee_bps"] == 50
    assert body["fee_percent"] == 0.5


def test_list_payments_endpoint(client):
    t = _tenant()
    sid = _sub(t.id)
    with SessionLocal() as db:
        db.add(OfftakerPayment(
            tenant_id=t.id, subscription_id=sid,
            invoice_number="2026-05", period_key="2026-05-31",
            amount_cents=5000, fee_cents=25, status="paid",
            customer_name="Town of Test",
            paid_at=datetime.utcnow(),
        ))
        db.commit()
    auth = f"Bearer {mint_session_for_tenant(t.id)}"
    r = client.get("/v1/array-operator/billing/payments",
                   headers={"Authorization": auth})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert body["payments"][0]["amount_usd"] == 50.0
    assert body["payments"][0]["fee_usd"] == 0.25
    assert body["payments"][0]["status"] == "paid"


# ─── email CTA ──────────────────────────────────────────────────────────────

def test_email_html_includes_pay_cta():
    from api.billing.delivery import _email_html
    match = _FakeMatch()
    sub = SimpleNamespace(
        customer_name="Town of Test", include_summary=False,
        tenant_id=None, auto_attach_gmp=False, gmp_invoice_pdf=None,
    )
    with patch("api.billing.delivery._offtaker_email_fields",
               return_value={"tenant_name": "Owner Co", "tenant_email": "o@x.com",
                             "signoff_t": None, "signoff_name": None,
                             "subject_t": None, "body_t": None}):
        subject, html, text = _email_html(
            match, sub, is_test=False,
            pay_url="https://checkout.stripe.com/c/pay/cs_abc")
    assert "Pay invoice securely" in html
    assert "https://checkout.stripe.com/c/pay/cs_abc" in html
    assert "Amount due" in html
    assert "Pay online" in text
    # Sky redesign tokens on AO offtaker emails
    assert "#dceef9" in html or "#10b981" in html


def test_email_html_test_banner_mentions_real_pay_button():
    from api.billing.delivery import _email_html
    match = _FakeMatch()
    sub = SimpleNamespace(
        customer_name="Town of Test", include_summary=False,
        tenant_id=None, auto_attach_gmp=False, gmp_invoice_pdf=None,
    )
    with patch("api.billing.delivery._offtaker_email_fields",
               return_value={"tenant_name": "Owner Co", "tenant_email": "o@x.com",
                             "signoff_t": None, "signoff_name": None,
                             "subject_t": None, "body_t": None}):
        _, html, _ = _email_html(
            match, sub, is_test=True,
            pay_url="https://checkout.stripe.com/c/pay/cs_test")
    assert "Test send" in html
    assert "Pay invoice securely" in html
    assert "same as offtakers will see" in html


def test_link_existing_connect_account_by_email(monkeypatch):
    t = _tenant(stripe_connect_account_id=None, stripe_connect_charges_enabled=False)
    # Override contact email to a known value for matching
    with SessionLocal() as db:
        tenant = db.get(Tenant, t.id)
        tenant.contact_email = "owner@linktest.example"
        tenant.stripe_connect_account_id = None
        tenant.stripe_connect_charges_enabled = False
        db.commit()

    fake_acct = {
        "id": "acct_linked_by_email",
        "email": "owner@linktest.example",
        "charges_enabled": True,
        "details_submitted": True,
        "metadata": {},
    }

    class _Page(dict):
        def __init__(self):
            super().__init__(data=[fake_acct], has_more=False)

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    with patch("api.billing.payments.stripe.Account.list", return_value=_Page()):
        with SessionLocal() as db:
            tenant = db.get(Tenant, t.id)
            res = pay.link_existing_connect_account(db, tenant)
            db.refresh(tenant)
            assert res["ok"] and res["linked"]
            assert tenant.stripe_connect_account_id == "acct_linked_by_email"
            assert tenant.stripe_connect_charges_enabled is True


def test_friendly_connect_error_hides_stripe_raw():
    err = pay._friendly_connect_error(Exception(
        "You can only create new accounts if you've signed up for Connect, "
        "which you can do at https://dashboard.stripe.com/connect. "
        "Request req_tuyVo9YbNEm1XG"))
    assert err["error_code"] == "platform_connect_not_ready"
    assert "req_" not in err["error"]
    assert "dashboard.stripe.com" not in err["error"]
    assert "try again" in err["error"].lower() or "minutes" in err["error"].lower()


def test_send_payment_received_emails_calls_resend():
    sent = []
    def fake_send(**kw):
        sent.append(kw)
        return True
    # send_payment_received_emails does `from ..notify import _send_via_resend`
    # at call time, so patch the notify module attribute.
    with patch("api.notify._send_via_resend", side_effect=fake_send):
        res = pay.send_payment_received_emails({
            "offtaker_email": "off@example.com",
            "offtaker_name": "Town of Test",
            "owner_email": "owner@example.com",
            "owner_name": "Owner Co",
            "invoice_number": "2026-06",
            "period_key": "2026-06-30",
            "amount_cents": 10_000,
            "fee_cents": 50,
            "product": "array_operator",
        })
    assert res["sent"] is True
    assert res["offtaker"] is True
    assert res["owner"] is True
    assert len(sent) == 2
    subjects = [s["subject"] for s in sent]
    assert any("Payment received" in s for s in subjects)
    assert any("Paid" in s for s in subjects)
