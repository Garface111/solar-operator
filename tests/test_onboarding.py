"""
Tests for the 5-screen onboarding flow (api/onboarding.py + the shared Stripe
webhook in api/signup.py).

Covered:
  (a) checkout creates a pending tenant at stage='pending_payment'
  (b) status echoes the correct stage
  (c) a checkout.session.completed webhook advances stage → 'extension'
  (d) /complete moves stage → 'done' and fires the magic-link email

Stripe and all outbound email are mocked — no network, no real charges.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import api.onboarding as onboarding
import api.account as account
from api.db import SessionLocal
from api.models import Tenant


# ─── mocks ───────────────────────────────────────────────────────────────

@pytest.fixture()
def mocks(monkeypatch):
    """Mock Stripe checkout creation + every outbound email helper, recording
    calls so tests can assert on them."""
    calls = {"welcome": [], "magic_link": [], "internal_alert": []}

    def fake_session_create(**kwargs):
        # echo metadata back so the webhook test can read the token if needed
        return SimpleNamespace(
            url="https://checkout.stripe.test/cs_test_123",
            metadata=kwargs.get("metadata", {}),
        )

    monkeypatch.setattr(onboarding.stripe.checkout.Session, "create", fake_session_create)
    monkeypatch.setattr(onboarding, "STRIPE_SECRET_KEY", "sk_test_dummy")

    monkeypatch.setattr(
        onboarding, "send_welcome_email",
        lambda **kw: calls["welcome"].append(kw) or True,
    )
    monkeypatch.setattr(
        onboarding, "send_internal_alert",
        lambda *a, **k: calls["internal_alert"].append((a, k)) or True,
    )

    # Magic-link goes out via account._send_via_resend — record it.
    def fake_resend(**kw):
        calls["magic_link"].append(kw)
        return True

    monkeypatch.setattr(account, "_send_via_resend", fake_resend)
    return calls


# ─── helpers ─────────────────────────────────────────────────────────────

def _do_checkout(client, email="op@example.com"):
    resp = client.post("/v1/onboarding/checkout", json={
        "email": email,
        "full_name": "Olivia Operator",
        "company": "Green Ridge Solar",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fire_checkout_webhook(client, onboarding_token, event_id="evt_test_1"):
    """Simulate Stripe POSTing checkout.session.completed for this tenant."""
    event = {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "metadata": {"onboarding_token": onboarding_token},
            "customer": "cus_test_123",
            "subscription": "sub_test_123",
            "customer_email": "op@example.com",
        }},
    }
    resp = client.post(
        "/v1/stripe/webhook",
        content=json.dumps(event),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── (start) no-upfront-payment signup creates a live trial ──────────────

def test_start_creates_trialing_tenant_no_stripe(client, mocks, monkeypatch):
    """POST /v1/onboarding/start creates a live, trialing tenant with
    trial_ends_at set and NO Stripe calls."""
    # Blow up if anyone tries to hit Stripe Checkout from the no-card flow.
    def _boom(**kwargs):
        raise AssertionError("start must not call Stripe Checkout")
    monkeypatch.setattr(onboarding.stripe.checkout.Session, "create", _boom)

    resp = client.post("/v1/onboarding/start", json={
        "email": "start@example.com",
        "full_name": "Stella Start",
        "company": "Start Solar",
        "array_count": 7,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["onboarding_token"]
    assert token and len(token) >= 30
    assert body["tenant_id"].startswith("ten_")

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.onboarding_token == token)
        ).scalar_one()
        assert t.active is True
        assert t.subscription_status == "trialing"
        assert t.onboarding_stage == "extension"
        assert t.trial_ends_at is not None
        assert t.stripe_customer_id is None
        assert t.stripe_payment_method_id is None
        assert t.onboarding_array_estimate == 7
        assert t.contact_email == "start@example.com"
        # Default product is the NEPOOL verifier.
        assert t.product == "nepool"


def test_start_array_operator_product_same_trial(client, mocks, monkeypatch):
    """An Array Operator owner signup gets product='array_operator' and the
    IDENTICAL 14-day no-card trial (trial is product-agnostic)."""
    def _boom(**kwargs):
        raise AssertionError("start must not call Stripe Checkout")
    monkeypatch.setattr(onboarding.stripe.checkout.Session, "create", _boom)

    resp = client.post("/v1/onboarding/start", json={
        "email": "owner@example.com",
        "full_name": "Olive Owner",
        "company": "Owner Arrays",
        "array_count": 3,
        "product": "array_operator",
    })
    assert resp.status_code == 200, resp.text
    token = resp.json()["onboarding_token"]

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.onboarding_token == token)
        ).scalar_one()
        assert t.product == "array_operator"
        # Same trial mechanics as NEPOOL — no card, trialing, 14-day clock.
        assert t.subscription_status == "trialing"
        assert t.trial_ends_at is not None
        assert t.stripe_payment_method_id is None


def test_start_with_duplicate_active_email_returns_409_not_500(client, mocks):
    """Regression: legacy/raced data can leave >1 ACTIVE tenant on one email.

    The duplicate-email guard must not use scalar_one_or_none() (which raises
    MultipleResultsFound -> 500 and wedges signup permanently for that email).
    A second active row simply means the account is taken -> clean 409.
    """
    from datetime import timedelta
    from api.onboarding import (
        gen_tenant_id, gen_tenant_key, gen_onboarding_token, now,
    )

    email = "dupe@example.com"
    # Seed TWO active tenants on the same email (what production drifted into).
    with SessionLocal() as db:
        for _ in range(2):
            db.add(Tenant(
                id=gen_tenant_id(), name="Dupe Co", contact_email=email,
                operator_name="Dupe Owner", company_name="Dupe Co",
                tenant_key=gen_tenant_key(), plan="standard", active=True,
                created_at=now(), product="array_operator",
                subscription_status="trialing",
                trial_ends_at=now() + timedelta(days=14),
                onboarding_token=gen_onboarding_token(),
                onboarding_stage="extension",
            ))
        db.commit()

    resp = client.post("/v1/onboarding/start", json={
        "email": email,
        "full_name": "Dexter Dupe",
        "company": "Dupe Co",
        "array_count": 2,
        "product": "array_operator",
    })
    assert resp.status_code == 409, resp.text
    assert "already exists" in resp.json()["detail"].lower()


def test_start_blocks_inactive_duplicate_same_product(client, mocks):
    """The duplicate guard must fire even when the existing tenant is INACTIVE.
    Before this fix it only checked active==True, so an email with an inactive
    AO tenant could mint a SECOND AO tenant — the multi-account root cause behind
    the magic-link/password 'wrong account' glitches."""
    from datetime import timedelta
    from api.onboarding import gen_tenant_id, gen_tenant_key, gen_onboarding_token, now
    email = "inactive_dupe@example.com"
    with SessionLocal() as db:
        db.add(Tenant(
            id=gen_tenant_id(), name="Inactive Co", contact_email=email,
            tenant_key=gen_tenant_key(), plan="standard", active=False,
            created_at=now(), product="array_operator",
            subscription_status="canceled",
            onboarding_token=gen_onboarding_token(), onboarding_stage="extension",
        ))
        db.commit()
    resp = client.post("/v1/onboarding/start", json={
        "email": email, "full_name": "Iris Inactive", "array_count": 1,
        "product": "array_operator",
    })
    assert resp.status_code == 409, resp.text
    # A DEACTIVATED account is recoverable: the copy must read as welcome-back /
    # reactivate, NOT a hard "account already exists / lost access" wall.
    detail = resp.json()["detail"].lower()
    assert "welcome back" in detail
    assert "reactivate" in detail
    assert "already exists" not in detail


def test_start_allows_same_email_different_product(client, mocks):
    """One person can legitimately own a NEPOOL account AND an Array Operator
    account on the same email — the guard is per-product, not per-email."""
    from datetime import timedelta
    from api.onboarding import gen_tenant_id, gen_tenant_key, gen_onboarding_token, now
    email = "crossproduct@example.com"
    with SessionLocal() as db:
        db.add(Tenant(
            id=gen_tenant_id(), name="NEPOOL Co", contact_email=email,
            tenant_key=gen_tenant_key(), plan="standard", active=True,
            created_at=now(), product="nepool", subscription_status="trialing",
            trial_ends_at=now() + timedelta(days=14),
            onboarding_token=gen_onboarding_token(), onboarding_stage="extension",
        ))
        db.commit()
    # Signing up for array_operator on the SAME email must succeed.
    resp = client.post("/v1/onboarding/start", json={
        "email": email, "full_name": "Cross Product", "array_count": 1,
        "product": "array_operator",
    })
    assert resp.status_code == 200, resp.text


# ─── (a) checkout shim now creates a live trial (no card) ────────────────

def test_checkout_shim_creates_trialing_tenant(client, mocks):
    """The deprecated /checkout shim mirrors /start: a live trialing tenant,
    checkout_url=None (no Stripe Checkout to redirect to)."""
    body = _do_checkout(client, email="a@example.com")
    assert body["checkout_url"] is None
    token = body["onboarding_token"]
    assert token and len(token) >= 30

    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.onboarding_token == token)
        ).scalar_one()
        assert t.active is True
        assert t.onboarding_stage == "extension"
        assert t.subscription_status == "trialing"
        assert t.trial_ends_at is not None
        assert t.contact_email == "a@example.com"


# ─── (b) status returns the correct stage ────────────────────────────────

def test_status_returns_stage(client, mocks):
    token = _do_checkout(client, email="b@example.com")["onboarding_token"]
    resp = client.get("/v1/onboarding/status", params={"token": token})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # No-upfront-payment: the tenant is live the moment signup starts.
    assert data["stage"] == "extension"
    assert data["active"] is True
    assert data["activation_code"] is not None
    # A "Your first client" placeholder is seeded at activation (now signup-time).
    assert data["clients_count"] == 1


def test_status_unknown_token_404(client, mocks):
    resp = client.get("/v1/onboarding/status", params={"token": "nope"})
    assert resp.status_code == 404


# ─── (c) webhook advances stage → extension ──────────────────────────────

def test_webhook_advances_to_extension(client, mocks):
    token = _do_checkout(client, email="c@example.com")["onboarding_token"]

    result = _fire_checkout_webhook(client, token, event_id="evt_c")
    assert result.get("tenant_activated")

    resp = client.get("/v1/onboarding/status", params={"token": token})
    data = resp.json()
    assert data["stage"] == "extension"
    assert data["active"] is True
    assert data["activation_code"] is not None  # tenant_key now exposed

    # Webhook must NOT have sent the welcome email (deferred to /complete).
    assert mocks["welcome"] == []


# ─── (d) complete → done + magic link ────────────────────────────────────

def test_complete_sends_magic_link_and_finishes(client, mocks):
    email = "d@example.com"
    token = _do_checkout(client, email=email)["onboarding_token"]
    _fire_checkout_webhook(client, token, event_id="evt_d")

    # Advance through the extension + clients steps.
    assert client.post("/v1/onboarding/extension-installed",
                       params={"token": token}).status_code == 200
    clients_resp = client.post(
        "/v1/onboarding/clients",
        params={"token": token},
        json=[{
            "name": "Maple Street Co-op",
            "contact_email": "maple@example.com",
            "gmp_autopopulate": False,
            "arrays": [{"name": "Maple Roof", "nepool_gis_id": "53984"}],
        }],
    )
    assert clients_resp.status_code == 200, clients_resp.text
    assert len(clients_resp.json()["client_ids"]) == 1

    # Complete.
    resp = client.post("/v1/onboarding/complete", params={"token": token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["magic_link_email_sent"] is True

    # Stage is now done.
    status = client.get("/v1/onboarding/status", params={"token": token}).json()
    assert status["stage"] == "done"
    assert status["clients_count"] == 1
    assert status["arrays_count"] == 1

    # Welcome email fired here (not at the webhook), and a magic link went out.
    assert len(mocks["welcome"]) == 1
    assert mocks["welcome"][0]["to"] == email
    assert len(mocks["magic_link"]) == 1
    assert mocks["magic_link"][0]["to"] == email
    # Magic link lands on the buyer-facing dashboard URL (solaroperator.org/accounts,
    # Netlify-proxied to the /app/ mount), which exchanges the one-time login token
    # for a session via /v1/auth/verify.
    assert "/accounts/?token=" in mocks["magic_link"][0]["text"]


# ─── /complete with operator-chosen password (June 6 2026) ────────────────────
# Ford's note: "email me a link" delivery was too slow on second-device sign-in.
# Onboarding now lets the operator set a password on /info; that password is
# posted to /v1/onboarding/complete and bcrypt-hashed into tenants.password_hash.
# Then password-login works the moment they leave the wizard.

def test_complete_with_password_sets_hash_and_allows_password_login(client, mocks):
    email = "pw@example.com"
    token = _do_checkout(client, email=email)["onboarding_token"]
    _fire_checkout_webhook(client, token, event_id="evt_pw")

    assert client.post("/v1/onboarding/extension-installed",
                       params={"token": token}).status_code == 200
    client.post(
        "/v1/onboarding/clients",
        params={"token": token},
        json=[{
            "name": "Pine Acres",
            "contact_email": "pine@example.com",
            "gmp_autopopulate": False,
            "arrays": [{"name": "Pine Roof", "nepool_gis_id": "12345"}],
        }],
    )

    resp = client.post(
        "/v1/onboarding/complete",
        params={"token": token},
        json={"password": "hunter2hunter2"},
    )
    assert resp.status_code == 200, resp.text

    # Password-login works immediately with the same email + password.
    login = client.post(
        "/v1/auth/password-login",
        json={"email": email, "password": "hunter2hunter2"},
    )
    assert login.status_code == 200, login.text
    assert "session_token" in login.json()


def test_complete_with_weak_password_400s(client, mocks):
    email = "weak@example.com"
    token = _do_checkout(client, email=email)["onboarding_token"]
    _fire_checkout_webhook(client, token, event_id="evt_weak")

    assert client.post("/v1/onboarding/extension-installed",
                       params={"token": token}).status_code == 200
    client.post(
        "/v1/onboarding/clients",
        params={"token": token},
        json=[{
            "name": "Weak Acres",
            "contact_email": "weak@example.com",
            "gmp_autopopulate": False,
            "arrays": [],
        }],
    )

    # 7 chars, no digit → 400
    resp = client.post(
        "/v1/onboarding/complete",
        params={"token": token},
        json={"password": "abcdefg"},
    )
    assert resp.status_code == 400, resp.text


# ─── (e) Screen 4 reconciles Stripe quantity to the real array count ──────

def test_clients_reconciles_stripe_quantity(client, mocks, monkeypatch):
    """2 clients × 3 arrays = 6 arrays → Stripe per-array item bumped to qty 6."""
    # The constant lives in stripe_helpers (where reconcile_subscription_quantity
    # reads it). Patch both onboarding (used at checkout creation) and
    # stripe_helpers (used inside the reconcile call) for correctness.
    monkeypatch.setattr(onboarding, "STRIPE_ARRAY_PRICE_ID", "price_array_test")
    from api import stripe_helpers as _sh
    monkeypatch.setattr(_sh, "STRIPE_ARRAY_PRICE_ID", "price_array_test")

    # Stripe subscription has two items: the one-time setup and the recurring
    # per-array line. Only the latter (matching STRIPE_ARRAY_PRICE_ID) is touched.
    def fake_sub_retrieve(sub_id):
        assert sub_id == "sub_test_123"
        return {"items": {"data": [
            {"id": "si_setup", "price": {"id": "price_setup_test"}},
            {"id": "si_array", "price": {"id": "price_array_test"}},
        ]}}

    modify_calls = []

    def fake_item_modify(item_id, **kwargs):
        modify_calls.append((item_id, kwargs))
        return SimpleNamespace(id=item_id, **kwargs)

    monkeypatch.setattr(onboarding.stripe.Subscription, "retrieve", fake_sub_retrieve)
    monkeypatch.setattr(onboarding.stripe.SubscriptionItem, "modify", fake_item_modify)

    token = _do_checkout(client, email="e@example.com")["onboarding_token"]
    _fire_checkout_webhook(client, token, event_id="evt_e")

    two_by_three = [
        {
            "name": f"Client {n}",
            "contact_email": f"client{n}@example.com",
            "gmp_autopopulate": False,
            "arrays": [{"name": f"Array {n}-{a}"} for a in range(3)],
        }
        for n in range(2)
    ]
    resp = client.post("/v1/onboarding/clients",
                       params={"token": token}, json=two_by_three)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["client_ids"]) == 2

    # The recurring per-array item — and only it — was bumped to quantity=6.
    assert len(modify_calls) == 1
    item_id, kwargs = modify_calls[0]
    assert item_id == "si_array"
    assert kwargs["quantity"] == 6
    assert kwargs["proration_behavior"] == "create_prorations"

    # Failure path stayed quiet — no internal alert on the happy path.
    assert mocks["internal_alert"] == []


# ─── (f) paid-but-inactive self-heal (W1-4) ──────────────────────────────

def _fake_paid_session(token, *, paid=True, customer="cus_heal", sub="sub_heal"):
    """A Stripe Checkout session object (dict-like) for reconcile tests."""
    return {
        "id": "cs_test_heal",
        "payment_status": "paid" if paid else "unpaid",
        "customer": customer,
        "subscription": sub,
        "metadata": {"onboarding_token": token},
    }


def test_reconcile_is_noop_for_active_tenant(client, mocks, monkeypatch):
    """No-upfront-payment: tenants are active from signup, so reconcile-checkout
    is a harmless no-op — it just echoes the live state (stale extension popups
    still call it; never 500/402 them)."""
    token = _do_checkout(client, email="heal@example.com")["onboarding_token"]

    # Even without a session_id (the stale-popup case), it must not error.
    resp = client.post("/v1/onboarding/reconcile-checkout",
                       params={"token": token})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["active"] is True
    assert data["stage"] == "extension"
    assert data["activation_code"] is not None


def test_extension_installed_advances_without_payment(client, mocks):
    """No-upfront-payment: 'I've installed it' just advances extension→clients;
    there is no payment gate (no 402)."""
    token = _do_checkout(client, email="click@example.com")["onboarding_token"]
    resp = client.post(
        "/v1/onboarding/extension-installed",
        params={"token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stage"] == "clients"


# ─── (f) V2 fuel selection persists through onboarding ────────────────────
# Regression: the wizard collects a per-client default fuel and a per-array
# fuel (wind/hydro/digester/storage), but the backend used to drop both —
# every onboarded array landed as 'solar'. These prove the backend now
# matches the onboarding payload.

def test_onboarding_persists_array_and_client_fuel(client, mocks):
    """Per-array fuel wins; a fuel-less array inherits the client default; the
    client's default_fuel_type is stored for the autopop path; garbage → solar."""
    from api.models import Client, Array

    token = _do_checkout(client, email="fuel@example.com")["onboarding_token"]
    assert client.post("/v1/onboarding/extension-installed",
                       params={"token": token}).status_code == 200

    resp = client.post(
        "/v1/onboarding/clients",
        params={"token": token},
        json=[
            {
                "name": "Ridgeline Wind",
                "gmp_autopopulate": False,
                "default_fuel_type": "wind",
                "arrays": [
                    # explicit per-array fuel overrides the client default
                    {"name": "Ridge Turbine A", "fuel_type": "hydro"},
                    # no per-array fuel → inherits the client's 'wind' default
                    {"name": "Ridge Turbine B"},
                    # garbage per-array fuel → falls back down the chain to the
                    # client's 'wind' default (never breaks the request)
                    {"name": "Ridge Turbine C", "fuel_type": "plutonium"},
                ],
            },
            {
                # No client default + garbage array fuel → floors at solar, so
                # report dispatch is never broken by bad input.
                "name": "Garbage In",
                "gmp_autopopulate": False,
                "arrays": [{"name": "Junk Array", "fuel_type": "plutonium"}],
            },
            {
                # Autopop client: sends NO arrays, only a default fuel. The
                # default must be stored so /v1/sync-created arrays inherit it.
                "name": "Otter Creek Hydro",
                "gmp_autopopulate": True,
                "gmp_email": "otter@example.com",
                "default_fuel_type": "digester",
                "arrays": [],
            },
        ],
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        arrays = {
            a.name: a.fuel_type
            for a in db.execute(select(Array)).scalars().all()
        }
        assert arrays["Ridge Turbine A"] == "hydro"   # per-array wins
        assert arrays["Ridge Turbine B"] == "wind"    # inherits client default
        assert arrays["Ridge Turbine C"] == "wind"    # garbage → client default
        assert arrays["Junk Array"] == "solar"        # garbage, no default → solar

        wind_client = db.execute(
            select(Client).where(Client.name == "Ridgeline Wind")
        ).scalar_one()
        assert wind_client.default_fuel_type == "wind"

        autopop_client = db.execute(
            select(Client).where(Client.name == "Otter Creek Hydro")
        ).scalar_one()
        # Stored for the autopop path even though no arrays were sent here.
        assert autopop_client.default_fuel_type == "digester"


def test_onboarding_solar_default_unchanged(client, mocks):
    """Omitting fuel entirely (the common solar case) still yields solar — the
    pre-V2 payload shape is byte-identical."""
    from api.models import Array

    token = _do_checkout(client, email="solar@example.com")["onboarding_token"]
    assert client.post("/v1/onboarding/extension-installed",
                       params={"token": token}).status_code == 200

    resp = client.post(
        "/v1/onboarding/clients",
        params={"token": token},
        json=[{
            "name": "Sunny Fields",
            "gmp_autopopulate": False,
            "arrays": [{"name": "South Roof", "nepool_gis_id": "53984"}],
        }],
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        arr = db.execute(
            select(Array).where(Array.name == "South Roof")
        ).scalar_one()
        assert arr.fuel_type == "solar"
