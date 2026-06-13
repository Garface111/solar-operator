"""Tests for Array Operator (owner-side) graduated per-kWh pricing.

Pins the owner kWh tier boundaries so a future TIERS edit can't silently change
what owners are billed. These numbers must match the live Stripe graduated
METERED price built by scripts/create_array_operator_prices.py.

Pricing (Jun 2026): 0.5¢/kWh up to 20,000 kWh/mo, 0.45¢ to 200,000, 0.40¢ above.
Amounts are DECIMAL cents (sub-cent rate), so compute_monthly_cents returns a
float that may be fractional.
"""
import math

from api.pricing_array_operator import (
    compute_monthly_cents,
    blended_unit_cents,
    stripe_tiers,
    FULL_UNIT_CENTS,
    SETUP_CENTS,
    TIERS,
)


def test_zero_and_negative_are_free():
    assert compute_monthly_cents(0) == 0.0
    assert compute_monthly_cents(-5) == 0.0
    assert compute_monthly_cents(None) == 0.0


def test_no_setup_fee_on_owner_side():
    assert SETUP_CENTS == 0


def test_headline_unit_is_full_tier():
    assert FULL_UNIT_CENTS == 0.50  # 0.5¢/kWh, in decimal cents


def test_residential_full_rate():
    # ~900 kWh/mo home array: all within the first 20k band @ 0.5¢ → 450¢ = $4.50
    assert math.isclose(compute_monthly_cents(900), 450.0)


def test_commercial_full_rate():
    # 99 kW commercial ~10,000 kWh/mo: still within first band → 5000¢ = $50.00
    assert math.isclose(compute_monthly_cents(10_000), 5000.0)


def test_first_band_top_boundary():
    # Exactly 20,000 kWh, all @ 0.5¢ → 10,000¢ = $100.00
    assert math.isclose(compute_monthly_cents(20_000), 10_000.0)


def test_second_band_crossing():
    # 20,000 @ 0.5¢ (=10,000¢) + 1 @ 0.45¢ = 10,000.45¢
    assert math.isclose(compute_monthly_cents(20_001), 10_000.45)


def test_third_band_crossing():
    # 20,000 @ 0.5¢ (=10,000¢) + 180,000 @ 0.45¢ (=81,000¢) = 91,000¢ at 200k
    assert math.isclose(compute_monthly_cents(200_000), 91_000.0)
    # 200,001st kWh enters the 0.40¢ fleet band
    assert math.isclose(compute_monthly_cents(200_001), 91_000.0 + 0.40)


def test_fleet_floor_is_marginal_040():
    big = compute_monthly_cents(1_000_000)
    prev = compute_monthly_cents(999_999)
    assert math.isclose(big - prev, 0.40)  # each marginal kWh past 200k = 0.4¢


def test_blended_unit_average():
    assert blended_unit_cents(0) == FULL_UNIT_CENTS
    # within the first band the blended rate equals the full rate
    assert math.isclose(blended_unit_cents(900), 0.50)


def test_stripe_tiers_shape_matches_table():
    # Sub-cent → unit_amount_decimal (string), NOT integer unit_amount.
    assert stripe_tiers() == [
        {"up_to": 20_000, "unit_amount_decimal": "0.5"},
        {"up_to": 200_000, "unit_amount_decimal": "0.45"},
        {"up_to": "inf", "unit_amount_decimal": "0.4"},
    ]


def test_tiers_table_is_ascending_and_terminated():
    bounds = [b for b, _ in TIERS]
    assert bounds[-1] is None  # final tier is infinity
    finite = [b for b in bounds if b is not None]
    assert finite == sorted(finite)
