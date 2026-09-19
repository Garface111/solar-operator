"""AO subscription line reconciliation under the unified regular product.

July 2026 retired monitoring/invoicing plan gating: all AO tenants retain
monitoring and pay for actual registered offtakers. Legacy plan labels must
not remove monitoring or create a phantom invoicing quantity for empty rosters.
Stripe is fully mocked; fixtures use real roster rows to exercise counting.
"""
from __future__ import annotations

import secrets
import pytest

from api.db import SessionLocal
from api.models import Tenant, BillingReportSubscription
from api import stripe_helpers

KWH = "price_kwh_test"
INV = "price_inv_test"


def _mk_tenant(plan, *, sub_id="sub_test123", offtaker_count=0):
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Plan Migration Test",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(10),
            plan="standard", active=True, product="array_operator",
            billing_plan=plan, stripe_subscription_id=sub_id,
        ))
        db.flush()  # parent must exist before subscription foreign keys
        for i in range(offtaker_count):
            db.add(BillingReportSubscription(
                tenant_id=tid, customer_name=f"Offtaker {i}", client_email=f"off{i}@example.test",
                formats=["pdf"], enabled=True))
        db.commit()
    return tid


def _run(monkeypatch, plan, existing_prices, *, sub_id="sub_test123", offtaker_count=0):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", KWH)
    monkeypatch.delenv("STRIPE_AO_NAMEPLATE_PRICE_ID", raising=False)
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

    tid = _mk_tenant(plan, sub_id=sub_id, offtaker_count=offtaker_count)
    stripe_helpers.migrate_ao_subscription_lines(tid)
    return calls


@pytest.mark.parametrize("legacy_plan", ["monitoring", "invoicing", "both", None])
def test_actual_roster_adds_invoicing_line_regardless_of_legacy_plan(monkeypatch, legacy_plan):
    calls = _run(monkeypatch, legacy_plan, existing_prices=[KWH], offtaker_count=3)
    assert [c["price"] for c in calls["create"]] == [INV]
    assert calls["create"][0]["quantity"] == 3
    assert calls["delete"] == []


def test_empty_roster_removes_invoicing_line(monkeypatch):
    calls = _run(monkeypatch, "both", existing_prices=[KWH, INV], offtaker_count=0)
    assert calls["create"] == []
    assert calls["delete"] == [f"si_{INV}"]


def test_legacy_invoicing_label_does_not_remove_monitoring(monkeypatch):
    calls = _run(monkeypatch, "invoicing", existing_prices=[KWH], offtaker_count=2)
    assert [c["price"] for c in calls["create"]] == [INV]
    assert calls["delete"] == []


def test_monitoring_restored_and_empty_invoicing_line_removed(monkeypatch):
    calls = _run(monkeypatch, "invoicing", existing_prices=[INV], offtaker_count=0)
    assert [c["price"] for c in calls["create"]] == [KWH]
    assert calls["delete"] == [f"si_{INV}"]


def test_existing_required_lines_are_a_noop(monkeypatch):
    calls = _run(monkeypatch, "both", existing_prices=[KWH, INV], offtaker_count=2)
    assert calls["create"] == []
    assert calls["delete"] == []


def test_invoicing_line_added_with_exact_offtaker_quantity(monkeypatch):
    calls = _run(monkeypatch, "invoicing", existing_prices=[KWH], offtaker_count=5)
    created = next(c for c in calls["create"] if c["price"] == INV)
    assert created["quantity"] == 5
    assert created.get("proration_behavior") == "create_prorations"


def test_no_live_subscription_is_a_noop(monkeypatch):
    calls = _run(monkeypatch, "both", existing_prices=[KWH], sub_id=None, offtaker_count=3)
    assert calls["create"] == []
    assert calls["delete"] == []


def test_zero_offtakers_never_creates_a_minimum_charge(monkeypatch):
    calls = _run(monkeypatch, "both", existing_prices=[KWH], offtaker_count=0)
    assert calls == {"create": [], "delete": []}
