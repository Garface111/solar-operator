"""AO plan-change Stripe line migration (stripe_helpers.migrate_ao_subscription_lines).

A paying customer who changes plan must have their subscription LINES change too —
add/remove the per-kWh meter and the per-offtaker invoicing line. Stripe is fully
mocked here (no network, no real billing); we assert which SubscriptionItem
create/delete calls happen per transition.
"""
from __future__ import annotations

import secrets

from api.db import SessionLocal
from api.models import Tenant
from api import stripe_helpers

KWH = "price_kwh_test"
INV = "price_inv_test"


def _mk_tenant(plan, *, sub_id="sub_test123"):
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Plan Migration Test",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(10),
            plan="standard", active=True, product="array_operator",
            billing_plan=plan, stripe_subscription_id=sub_id,
        ))
        db.commit()
    return tid


def _run(monkeypatch, plan, existing_prices, *, sub_id="sub_test123"):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", KWH)
    monkeypatch.setenv("STRIPE_AO_INVOICING_PRICE_ID", INV)
    monkeypatch.setenv("STRIPE_AO_INVOICING_SETUP_PRICE_ID", "")
    calls = {"create": [], "delete": []}
    items = [{"id": f"si_{p}", "price": {"id": p}} for p in existing_prices]

    monkeypatch.setattr(stripe_helpers.stripe.Subscription, "retrieve",
                        staticmethod(lambda sid: {"items": {"data": items}}))
    monkeypatch.setattr(stripe_helpers.stripe.SubscriptionItem, "create",
                        staticmethod(lambda **kw: calls["create"].append(kw) or {"id": "si_new"}))
    monkeypatch.setattr(stripe_helpers.stripe.SubscriptionItem, "delete",
                        staticmethod(lambda item_id, **kw: calls["delete"].append(item_id) or {"id": item_id}))

    tid = _mk_tenant(plan, sub_id=sub_id)
    stripe_helpers.migrate_ao_subscription_lines(tid)
    return calls


def test_monitoring_to_both_adds_invoicing_line(monkeypatch):
    calls = _run(monkeypatch, "both", existing_prices=[KWH])
    assert [c["price"] for c in calls["create"]] == [INV]
    assert calls["delete"] == []


def test_both_to_monitoring_removes_invoicing_line(monkeypatch):
    calls = _run(monkeypatch, "monitoring", existing_prices=[KWH, INV])
    assert calls["create"] == []
    assert calls["delete"] == [f"si_{INV}"]


def test_monitoring_to_invoicing_swaps_both_lines(monkeypatch):
    calls = _run(monkeypatch, "invoicing", existing_prices=[KWH])
    assert [c["price"] for c in calls["create"]] == [INV]   # add invoicing
    assert calls["delete"] == [f"si_{KWH}"]                 # drop meter


def test_invoicing_to_monitoring_swaps_back(monkeypatch):
    calls = _run(monkeypatch, "monitoring", existing_prices=[INV])
    assert [c["price"] for c in calls["create"]] == [KWH]
    assert calls["delete"] == [f"si_{INV}"]


def test_same_plan_is_a_noop(monkeypatch):
    calls = _run(monkeypatch, "both", existing_prices=[KWH, INV])
    assert calls["create"] == []
    assert calls["delete"] == []


def test_invoicing_line_added_with_offtaker_quantity(monkeypatch):
    calls = _run(monkeypatch, "invoicing", existing_prices=[KWH])
    created = next(c for c in calls["create"] if c["price"] == INV)
    assert created["quantity"] >= 1   # licensed line carries a quantity
    assert created.get("proration_behavior") == "create_prorations"


def test_no_live_subscription_is_a_noop(monkeypatch):
    # A trialing tenant (no stripe_subscription_id) → never touches Stripe.
    calls = _run(monkeypatch, "both", existing_prices=[KWH], sub_id=None)
    assert calls["create"] == []
    assert calls["delete"] == []
