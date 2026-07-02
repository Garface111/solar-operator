"""Tests for the Array Operator INVOICING plan (per-offtaker, licensed).

Pins the offtaker tier boundaries + the plan-detection logic so a future edit can't
silently change what an invoicing customer is billed. These numbers must match the
live Stripe graduated LICENSED price built by
scripts/create_ao_invoicing_tiered_price.py.

Plan (Ford, Jun 2026 — bulk-discounted, replaces the earlier flat $20/offtaker):
graduated volume tiers mirroring NEPOOL's 0/10/20/30% discount curve, re-keyed to
offtaker count: 1-10 @ $20, 11-25 @ $18 (10% off), 26-50 @ $16 (20% off), 51+ @ $14
(30% off). $250 setup.
"""
from api.pricing_ao_invoicing import (
    compute_monthly_cents,
    blended_unit_cents,
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
    assert PER_OFFTAKER_CENTS == 2_000    # $20/offtaker headline (first tier)
    assert SETUP_CENTS == 25_000          # $250 one-time setup (waivable)
    assert BASE_CENTS == 0                # no base/floor in this model
    assert BASE_INCLUDES_OFFTAKERS == 0


def test_zero_is_free():
    assert compute_monthly_cents(0) == 0
    assert compute_monthly_cents(None) == 0
    assert compute_monthly_cents(-3) == 0


def test_graduated_within_first_tier():
    # 1-10 offtakers: flat $20 each, same as the pre-discount behavior.
    assert compute_monthly_cents(1) == 2_000     # $20
    assert compute_monthly_cents(2) == 4_000     # $40
    assert compute_monthly_cents(4) == 8_000     # $80 (Paul's 4)
    assert compute_monthly_cents(10) == 20_000   # $200 (last full-price offtaker)


def test_graduated_across_tiers_no_cliff():
    # 11th offtaker: first 10 @ $20 + 1 @ $18 (10% off) = $218, not 11×$18.
    assert compute_monthly_cents(11) == 20_000 + 1_800
    # 12 offtakers → $236 (10 @ $20 + 2 @ $18).
    assert compute_monthly_cents(12) == 20_000 + 2 * 1_800
    # 25 offtakers: 10 @ $20 + 15 @ $18 = $470.
    assert compute_monthly_cents(25) == 20_000 + 15 * 1_800
    # 26th offtaker crosses into the 20%-off band: + 1 @ $16.
    assert compute_monthly_cents(26) == 20_000 + 15 * 1_800 + 1_600
    # 50 offtakers: 10@$20 + 15@$18 + 25@$16 = $200+$270+$400 = $870.
    assert compute_monthly_cents(50) == 20_000 + 15 * 1_800 + 25 * 1_600
    # 51st offtaker crosses into the 30%-off band: + 1 @ $14.
    assert compute_monthly_cents(51) == 20_000 + 15 * 1_800 + 25 * 1_600 + 1_400
    # 60 offtakers: prior 50-offtaker total + 10 @ $14 = $870 + $140 = $1,010.
    assert compute_monthly_cents(60) == (20_000 + 15 * 1_800 + 25 * 1_600) + 10 * 1_400


def test_blended_rate_reproduces_total():
    for n in (1, 4, 10, 11, 25, 26, 50, 51, 60, 200):
        total = compute_monthly_cents(n)
        blended = blended_unit_cents(n)
        assert round(blended * n) == total, (n, blended, total)
    # No offtakers yet → headline rate, not a divide-by-zero.
    assert blended_unit_cents(0) == PER_OFFTAKER_CENTS


def test_no_revenue_cliff_at_breakpoints():
    # The offtaker AT a breakpoint must never cost less than one BELOW it —
    # that would be a cliff (adding an offtaker reduces the bill).
    for n in (9, 10, 11, 24, 25, 26, 49, 50, 51):
        assert compute_monthly_cents(n + 1) > compute_monthly_cents(n)


def test_stripe_tiers_shape():
    assert stripe_tiers() == [
        {"up_to": 10, "unit_amount": 2_000},
        {"up_to": 25, "unit_amount": 1_800},
        {"up_to": 50, "unit_amount": 1_600},
        {"up_to": "inf", "unit_amount": 1_400},
    ]


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
    # not chosen yet → defaults to FULL functionality (commit 342a8ac removed the
    # forced plan-picker: a fresh trial shows everything, treated as "chosen" so the
    # operator is never blocked; billing still reads billing_plan directly and bills
    # the conservative monitoring default until they explicitly narrow their plan).
    assert f(None) == {"plan": "both", "plan_chosen": True,
                       "vendor_data": True, "invoicing": True}
    assert f("") == {"plan": "both", "plan_chosen": True,
                     "vendor_data": True, "invoicing": True}
    # NEPOOL tenants are ungated (no AO plan-picker).
    nep = ao_plan_features("nepool", None)
    assert nep["plan_chosen"] is True and nep["vendor_data"] is True and nep["invoicing"] is True
