"""Tests for the Array Operator INVOICING plan (per-offtaker, licensed).

Pins the offtaker tier boundaries + the plan-detection logic so a future edit can't
silently change what an invoicing customer is billed. These numbers must match the
live Stripe graduated LICENSED price built by scripts/create_ao_invoicing_price.py.

Plan (Ford, Jul 2026 — Path A growth reprice): graduated volume tiers with 0/10/20/30%
discount curve, headline $15/offtaker: 1-10 @ $15, 11-25 @ $13.50, 26-50 @ $12,
51+ @ $10.50. $250 setup. Online pay skim is separate (0.5% default).
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
    assert PER_OFFTAKER_CENTS == 1_500    # $15/offtaker headline (first tier)
    assert SETUP_CENTS == 25_000          # $250 one-time setup (waivable)
    assert BASE_CENTS == 0                # no base/floor in this model
    assert BASE_INCLUDES_OFFTAKERS == 0


def test_zero_is_free():
    assert compute_monthly_cents(0) == 0
    assert compute_monthly_cents(None) == 0
    assert compute_monthly_cents(-3) == 0


def test_graduated_within_first_tier():
    # 1-10 offtakers: flat $15 each.
    assert compute_monthly_cents(1) == 1_500     # $15
    assert compute_monthly_cents(2) == 3_000     # $30
    assert compute_monthly_cents(4) == 6_000     # $60
    assert compute_monthly_cents(10) == 15_000   # $150 (last full-price offtaker)


def test_graduated_across_tiers_no_cliff():
    # 11th offtaker: first 10 @ $15 + 1 @ $13.50 (10% off) = $163.50
    assert compute_monthly_cents(11) == 15_000 + 1_350
    # 12 offtakers → $177 (10 @ $15 + 2 @ $13.50).
    assert compute_monthly_cents(12) == 15_000 + 2 * 1_350
    # 25 offtakers: 10 @ $15 + 15 @ $13.50 = $352.50
    assert compute_monthly_cents(25) == 15_000 + 15 * 1_350
    # 26th offtaker crosses into the 20%-off band: + 1 @ $12.
    assert compute_monthly_cents(26) == 15_000 + 15 * 1_350 + 1_200
    # 50 offtakers: 10@$15 + 15@$13.50 + 25@$12 = $150+$202.50+$300 = $652.50
    assert compute_monthly_cents(50) == 15_000 + 15 * 1_350 + 25 * 1_200
    # 51st offtaker crosses into the 30%-off band: + 1 @ $10.50.
    assert compute_monthly_cents(51) == 15_000 + 15 * 1_350 + 25 * 1_200 + 1_050
    # 60 offtakers: prior 50 + 10 @ $10.50 = $652.50 + $105 = $757.50
    assert compute_monthly_cents(60) == (15_000 + 15 * 1_350 + 25 * 1_200) + 10 * 1_050


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
        {"up_to": 10, "unit_amount": 1_500},
        {"up_to": 25, "unit_amount": 1_350},
        {"up_to": 50, "unit_amount": 1_200},
        {"up_to": "inf", "unit_amount": 1_050},
    ]


def test_is_ao_invoicing_detection():
    # Jul 2026: every AO tenant bills offtakers (plan split retired).
    assert is_ao_invoicing("array_operator", "invoicing") is True
    assert is_ao_invoicing("array_operator", "both") is True
    assert is_ao_invoicing("array_operator", "monitoring") is True
    assert is_ao_invoicing("array_operator", None) is True
    assert is_ao_invoicing("array_operator", "") is True
    assert is_ao_invoicing("nepool", "invoicing") is False
    assert is_ao_invoicing(None, "both") is False
    assert is_array_operator("array_operator") is True


def test_is_ao_monitoring_detection():
    # Every AO tenant bills nameplate monitoring.
    assert is_ao_monitoring("array_operator", "monitoring") is True
    assert is_ao_monitoring("array_operator", "both") is True
    assert is_ao_monitoring("array_operator", None) is True
    assert is_ao_monitoring("array_operator", "invoicing") is True
    assert is_ao_monitoring("nepool", "monitoring") is False


def test_ao_plan_features_entitlements():
    def f(plan):
        return ao_plan_features("array_operator", plan)
    full = {"plan": "regular", "plan_chosen": True,
            "vendor_data": True, "invoicing": True}
    assert f("monitoring") == full
    assert f("invoicing") == full
    assert f("both") == full
    assert f(None) == full
    assert f("") == full
    nep = ao_plan_features("nepool", None)
    assert nep["plan_chosen"] is True and nep["vendor_data"] is True and nep["invoicing"] is True
