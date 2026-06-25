"""AO per-kW NAMEPLATE billing routing — the monitoring line prefers the nameplate
price (with quantity) and falls back to the legacy metered price only when the
nameplate price isn't configured. (tenant_nameplate_kw itself is verified against
Bruce's real 63-inverter fleet = 982.9 kW.)"""
from api import stripe_helpers as sh


def test_ao_monitoring_item_prefers_nameplate(monkeypatch):
    monkeypatch.setattr(sh, "tenant_nameplate_kw", lambda db, tid: 983)
    monkeypatch.setenv("STRIPE_AO_NAMEPLATE_PRICE_ID", "price_np")
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", "price_kwh")
    assert sh.ao_monitoring_item(None, "ten_x") == {"price": "price_np", "quantity": 983}


def test_ao_monitoring_item_quantity_floor(monkeypatch):
    monkeypatch.setattr(sh, "tenant_nameplate_kw", lambda db, tid: 0)
    monkeypatch.setenv("STRIPE_AO_NAMEPLATE_PRICE_ID", "price_np")
    # never quantity 0 (Stripe requires >=1)
    assert sh.ao_monitoring_item(None, "ten_x")["quantity"] == 1


def test_ao_monitoring_item_falls_back_to_metered(monkeypatch):
    monkeypatch.delenv("STRIPE_AO_NAMEPLATE_PRICE_ID", raising=False)
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", "price_kwh")
    assert sh.ao_monitoring_item(None, "ten_x") == {"price": "price_kwh"}


def test_ao_monitoring_item_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("STRIPE_AO_NAMEPLATE_PRICE_ID", raising=False)
    monkeypatch.delenv("STRIPE_AO_KWH_PRICE_ID", raising=False)
    monkeypatch.delenv("STRIPE_AO_ARRAY_PRICE_ID", raising=False)
    assert sh.ao_monitoring_item(None, "ten_x") is None


def test_array_price_id_for_product_ao_prefers_nameplate(monkeypatch):
    monkeypatch.setenv("STRIPE_AO_NAMEPLATE_PRICE_ID", "price_np")
    monkeypatch.setenv("STRIPE_AO_KWH_PRICE_ID", "price_kwh")
    assert sh.array_price_id_for_product("array_operator") == "price_np"


def test_array_price_id_for_product_nepool_unchanged(monkeypatch):
    monkeypatch.setenv("STRIPE_AO_NAMEPLATE_PRICE_ID", "price_np")
    monkeypatch.setenv("STRIPE_ARRAY_PRICE_ID", "price_array")
    assert sh.array_price_id_for_product("nepool") == "price_array"
