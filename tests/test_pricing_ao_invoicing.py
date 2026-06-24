"""Tests for the Array Operator INVOICING plan (per-offtaker, licensed).

Pins the offtaker tier boundaries + the plan-detection logic so a future edit can't
silently change what an invoicing customer is billed. These numbers must match the
live Stripe graduated LICENSED price built by scripts/create_ao_invoicing_price.py.

Plan (Jun 2026): $100/mo base incl 4 offtakers + $25/offtaker beyond, $250 setup.
Anchor: Paul Bozuwa, 4 offtakers, said $100/mo is reasonable.
"""
from api.pricing_ao_invoicing import (
    compute_monthly_cents,
    stripe_tiers,
    BASE_CENTS,
    BASE_INCLUDES_OFFTAKERS,
    PER_OFFTAKER_CENTS,
    SETUP_CENTS,
)
from api.stripe_helpers import (
    is_ao_invoicing,
    is_ao_monitoring,
    is_array_operator,
    ao_plan_features,
)


def test_constants():
    # Flat $20/offtaker, no base/floor (Ford, Jun 2026).
    assert PER_OFFTAKER_CENTS == 2_000    # $20/offtaker
    assert SETUP_CENTS == 25_000          # $250 one-time setup (waivable)
    assert BASE_CENTS == 0                # no base in the flat model
    assert BASE_INCLUDES_OFFTAKERS == 0


def test_zero_is_free():
    assert compute_monthly_cents(0) == 0
    assert compute_monthly_cents(None) == 0
    assert compute_monthly_cents(-3) == 0


def test_flat_per_offtaker():
    # Every offtaker is a flat $20 — no base, no floor.
    assert compute_monthly_cents(1) == 2_000     # $20
    assert compute_monthly_cents(2) == 4_000     # $40
    assert compute_monthly_cents(4) == 8_000     # $80 (Paul's 4)
    assert compute_monthly_cents(5) == 10_000    # $100
    assert compute_monthly_cents(10) == 20_000   # $200
    assert compute_monthly_cents(20) == 40_000   # $400


def test_stripe_tiers_shape():
    # A single flat per-unit tier: every offtaker at $20.
    assert stripe_tiers() == [{"up_to": "inf", "unit_amount": 2_000}]


def test_is_ao_invoicing_detection():
    # invoicing line billed on plan 'invoicing' OR 'both'.
    assert is_ao_invoicing("array_operator", "invoicing") is True
    assert is_ao_invoicing("array_operator", "both") is True
    assert is_ao_invoicing("array_operator", "INVOICING") is True   # case-insensitive
    assert is_ao_invoicing("array_operator", " invoicing ") is True  # trimmed
    # monitoring / default / unknown — no invoicing line.
    assert is_ao_invoicing("array_operator", "monitoring") is False
    assert is_ao_invoicing("array_operator", None) is False
    assert is_ao_invoicing("array_operator", "") is False
    # NEPOOL is never invoicing, regardless of billing_plan.
    assert is_ao_invoicing("nepool", "invoicing") is False
    assert is_ao_invoicing(None, "both") is False
    assert is_array_operator("array_operator") is True


def test_is_ao_monitoring_detection():
    # per-kWh meter on 'monitoring', 'both', or the AO default (null/"").
    assert is_ao_monitoring("array_operator", "monitoring") is True
    assert is_ao_monitoring("array_operator", "both") is True
    assert is_ao_monitoring("array_operator", None) is True   # AO default
    assert is_ao_monitoring("array_operator", "") is True
    # invoicing-only → no monitoring meter.
    assert is_ao_monitoring("array_operator", "invoicing") is False
    # NEPOOL never bills on the AO meter.
    assert is_ao_monitoring("nepool", "monitoring") is False


def test_ao_plan_features_entitlements():
    def f(plan):
        return ao_plan_features("array_operator", plan)
    # monitoring → vendor data only
    assert f("monitoring") == {"plan": "monitoring", "plan_chosen": True,
                               "vendor_data": True, "invoicing": False}
    # invoicing → offtaker only
    assert f("invoicing") == {"plan": "invoicing", "plan_chosen": True,
                              "vendor_data": False, "invoicing": True}
    # both → everything
    assert f("both") == {"plan": "both", "plan_chosen": True,
                         "vendor_data": True, "invoicing": True}
    # not chosen yet → plan_chosen False, both features off (the login picker prompts)
    assert f(None) == {"plan": None, "plan_chosen": False,
                       "vendor_data": False, "invoicing": False}
    assert f("") == {"plan": None, "plan_chosen": False,
                     "vendor_data": False, "invoicing": False}
    # NEPOOL tenants are ungated (no AO plan-picker).
    nep = ao_plan_features("nepool", None)
    assert nep["plan_chosen"] is True and nep["vendor_data"] is True and nep["invoicing"] is True
