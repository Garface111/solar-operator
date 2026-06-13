"""Tests for product-aware Stripe price routing (api/stripe_helpers).

Ensures Array Operator tenants bill on the AO price and NEPOOL tenants keep the
NEPOOL price, and that a missing AO price falls back safely.
"""
import api.stripe_helpers as sh


def test_nepool_product_uses_nepool_price(monkeypatch):
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_nepool")
    monkeypatch.setenv("STRIPE_AO_ARRAY_PRICE_ID", "price_ao")
    assert sh.array_price_id_for_product("nepool") == "price_nepool"
    assert sh.array_price_id_for_product(None) == "price_nepool"
    assert sh.array_price_id_for_product("legacy_x") == "price_nepool"


def test_array_operator_uses_ao_price(monkeypatch):
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_nepool")
    monkeypatch.setenv("STRIPE_AO_ARRAY_PRICE_ID", "price_ao")
    assert sh.array_price_id_for_product("array_operator") == "price_ao"


def test_array_operator_falls_back_and_alerts_when_unset(monkeypatch):
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_nepool")
    monkeypatch.delenv("STRIPE_AO_ARRAY_PRICE_ID", raising=False)
    alerts = []
    monkeypatch.setattr(sh, "send_internal_alert",
                        lambda subj, body: alerts.append((subj, body)))
    # Falls back to NEPOOL price rather than returning empty/broken.
    assert sh.array_price_id_for_product("array_operator") == "price_nepool"
    assert len(alerts) == 1
    assert "Array Operator price id missing" in alerts[0][0]
