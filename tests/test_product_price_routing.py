"""Tests for product-aware Stripe price routing (api/stripe_helpers).

Ensures Array Operator tenants bill on the AO per-kWh price and NEPOOL tenants
keep the per-array price, and that a missing AO price falls back safely.
"""
import api.stripe_helpers as sh


def test_nepool_product_uses_nepool_price(monkeypatch):
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_nepool")
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", "price_ao_kwh")
    assert sh.array_price_id_for_product("nepool") == "price_nepool"
    assert sh.array_price_id_for_product(None) == "price_nepool"
    assert sh.array_price_id_for_product("legacy_x") == "price_nepool"


def test_array_operator_uses_ao_kwh_price(monkeypatch):
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_nepool")
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", "price_ao_kwh")
    monkeypatch.delenv("STRIPE_AO_ARRAY_PRICE_ID", raising=False)
    assert sh.array_price_id_for_product("array_operator") == "price_ao_kwh"


def test_array_operator_legacy_env_var_still_read(monkeypatch):
    # A half-migrated environment that only has the OLD var set must still route
    # AO tenants to it rather than falling back to NEPOOL.
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_nepool")
    monkeypatch.delenv("STRIPE_AO_KWH_PRICE_ID", raising=False)
    monkeypatch.setenv("STRIPE_AO_ARRAY_PRICE_ID", "price_ao_legacy")
    assert sh.array_price_id_for_product("array_operator") == "price_ao_legacy"


def test_array_operator_falls_back_and_alerts_when_unset(monkeypatch):
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_nepool")
    monkeypatch.delenv("STRIPE_AO_KWH_PRICE_ID", raising=False)
    monkeypatch.delenv("STRIPE_AO_ARRAY_PRICE_ID", raising=False)
    alerts = []
    monkeypatch.setattr(sh, "send_internal_alert",
                        lambda subj, body: alerts.append((subj, body)))
    # Falls back to NEPOOL price rather than returning empty/broken.
    assert sh.array_price_id_for_product("array_operator") == "price_nepool"
    assert len(alerts) == 1
    assert "price id missing" in alerts[0][0]


def test_is_array_operator_helper():
    assert sh.is_array_operator("array_operator") is True
    assert sh.is_array_operator("nepool") is False
    assert sh.is_array_operator(None) is False
