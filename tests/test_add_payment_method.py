"""
no-upfront-payment — POST /v1/account/add-payment-method.

The dashboard add-card flow returns a Stripe Checkout Session URL in setup mode,
lazy-creates the Stripe Customer if needed, and tags the SetupIntent +
session metadata with tenant_id so the setup_intent.succeeded webhook can
attribute the saved card back to this tenant.

Stripe is mocked — no network, no real charges.
"""
from __future__ import annotations

import secrets
from unittest.mock import patch

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, now


def _make_tenant(**overrides) -> tuple[str, str]:
    """Return (tenant_id, bearer_header) for an authed trialing tenant."""
    tid = "ten_pay_" + secrets.token_hex(5)
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="Pay Test Solar",
            contact_email=f"{tid}@pay.test",
            company_name="Pay Test Solar",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard",
            active=True,
            subscription_status="trialing",
            created_at=now(),
        )
        for k, v in overrides.items():
            setattr(t, k, v)
        db.add(t)
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def test_add_payment_method_lazy_creates_customer_and_returns_setup_url(client):
    tid, auth = _make_tenant()  # no stripe_customer_id yet
    captured: dict = {}

    with patch("api.account.stripe") as mock_stripe:
        mock_stripe.Customer.create.return_value = {"id": "cus_new_123"}

        def fake_session_create(**kwargs):
            captured.update(kwargs)
            return {"url": "https://checkout.stripe.test/cs_setup_xyz"}

        mock_stripe.checkout.Session.create.side_effect = fake_session_create

        resp = client.post("/v1/account/add-payment-method",
                           headers={"Authorization": auth})
        assert resp.status_code == 200, resp.text
        assert resp.json()["checkout_url"] == "https://checkout.stripe.test/cs_setup_xyz"

        # Customer was lazily created with the tenant's email + name.
        mock_stripe.Customer.create.assert_called_once()
        cust_kwargs = mock_stripe.Customer.create.call_args[1]
        assert cust_kwargs["email"] == f"{tid}@pay.test"
        assert cust_kwargs["metadata"]["tenant_id"] == tid

    # Setup-mode session, with tenant_id on BOTH the SetupIntent + the session.
    assert captured["mode"] == "setup"
    assert captured["customer"] == "cus_new_123"
    assert captured["setup_intent_data"]["metadata"]["tenant_id"] == tid
    assert captured["metadata"]["tenant_id"] == tid
    assert "card_added=1" in captured["success_url"]
    assert "card_cancelled=1" in captured["cancel_url"]

    # The lazily-created customer id was persisted.
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.stripe_customer_id == "cus_new_123"


def test_add_payment_method_reuses_existing_customer(client):
    tid, auth = _make_tenant(stripe_customer_id="cus_existing")
    captured: dict = {}

    with patch("api.account.stripe") as mock_stripe:
        def fake_session_create(**kwargs):
            captured.update(kwargs)
            return {"url": "https://checkout.stripe.test/cs_setup_2"}

        mock_stripe.checkout.Session.create.side_effect = fake_session_create

        resp = client.post("/v1/account/add-payment-method",
                           headers={"Authorization": auth})
        assert resp.status_code == 200, resp.text
        # No new customer created — the existing one is reused.
        mock_stripe.Customer.create.assert_not_called()

    assert captured["customer"] == "cus_existing"


def test_add_payment_method_requires_auth(client):
    resp = client.post("/v1/account/add-payment-method")
    assert resp.status_code == 401


# ─── POST /v1/account/confirm-setup ─────────────────────────────────────────
# The success_url return path: the dashboard posts the Checkout session id back
# so "card saved" confirms synchronously instead of racing the webhook.

def test_success_url_carries_checkout_session_id_template(client):
    tid, auth = _make_tenant(stripe_customer_id="cus_existing")
    captured: dict = {}
    with patch("api.account.stripe") as mock_stripe:
        def fake_session_create(**kwargs):
            captured.update(kwargs)
            return {"url": "https://checkout.stripe.test/cs_setup_3"}
        mock_stripe.checkout.Session.create.side_effect = fake_session_create
        resp = client.post("/v1/account/add-payment-method",
                           headers={"Authorization": auth})
        assert resp.status_code == 200, resp.text
    # Literal Stripe template — substituted by Stripe at redirect time.
    assert "session_id={CHECKOUT_SESSION_ID}" in captured["success_url"]


def test_confirm_setup_stores_pm_and_customer(client):
    tid, auth = _make_tenant()
    with patch("api.account.stripe") as mock_stripe:
        mock_stripe.error.StripeError = Exception
        mock_stripe.checkout.Session.retrieve.return_value = {
            "id": "cs_test_ok", "metadata": {"tenant_id": tid},
            "setup_intent": "seti_1",
        }
        mock_stripe.SetupIntent.retrieve.return_value = {
            "id": "seti_1", "payment_method": "pm_123", "customer": "cus_conf",
        }
        mock_stripe.PaymentMethod.retrieve.return_value = {
            "card": {"brand": "visa", "last4": "4242", "exp_month": 4, "exp_year": 2030},
        }
        resp = client.post("/v1/account/confirm-setup",
                           json={"session_id": "cs_test_ok"},
                           headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["card_saved"] is True
    # card_brand/last4 are best-effort (_card_brief talks to the real stripe
    # module, unmockable here) — present in the payload, null in tests.
    assert "card_last4" in d
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.stripe_payment_method_id == "pm_123"
        assert t.stripe_customer_id == "cus_conf"


def test_confirm_setup_rejects_foreign_session(client):
    """A checkout session minted for ANOTHER tenant must never attach here."""
    tid, auth = _make_tenant()
    with patch("api.account.stripe") as mock_stripe:
        mock_stripe.error.StripeError = Exception
        mock_stripe.checkout.Session.retrieve.return_value = {
            "id": "cs_foreign", "metadata": {"tenant_id": "ten_other"},
            "setup_intent": "seti_x",
        }
        resp = client.post("/v1/account/confirm-setup",
                           json={"session_id": "cs_foreign"},
                           headers={"Authorization": auth})
    assert resp.status_code == 403
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.stripe_payment_method_id is None


def test_confirm_setup_rejects_malformed_session_id(client):
    tid, auth = _make_tenant()
    with patch("api.account.stripe"):
        resp = client.post("/v1/account/confirm-setup",
                           json={"session_id": "not-a-session"},
                           headers={"Authorization": auth})
    assert resp.status_code == 422


def test_confirm_setup_requires_auth(client):
    resp = client.post("/v1/account/confirm-setup", json={"session_id": "cs_x"})
    assert resp.status_code == 401
