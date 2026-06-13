"""Tests for Array Operator (owner-side) graduated pricing.

Pins the owner tier boundaries so a future TIERS edit can't silently change
what owners are billed. These numbers must match the live Stripe graduated
price built by scripts/create_array_operator_prices.py.

Option B (Jun 2026): 1st array FREE, then $9 (2-10) / $8 (11-50) / $6.50 (51+).
"""
from api.pricing_array_operator import (
    compute_monthly_cents,
    blended_unit_cents,
    stripe_tiers,
    FULL_UNIT_CENTS,
    SETUP_CENTS,
)


def test_zero_and_negative_are_free():
    assert compute_monthly_cents(0) == 0
    assert compute_monthly_cents(-5) == 0


def test_first_array_is_free():
    # The residential wedge: one array costs nothing.
    assert compute_monthly_cents(1) == 0


def test_no_setup_fee_on_owner_side():
    assert SETUP_CENTS == 0


def test_headline_unit_is_first_paid_tier():
    assert FULL_UNIT_CENTS == 900  # $9.00


def test_full_rate_within_paid_band():
    # arrays 2-10 are $9 each; array 1 free.
    assert compute_monthly_cents(2) == 900           # 1 free + 1 @ $9
    assert compute_monthly_cents(10) == 9 * 900       # 1 free + 9 @ $9 = 8100


def test_prosumer_band_crossing():
    # 1 free + 9@$9 + 1@$8  (the 11th array enters the $8 band)
    assert compute_monthly_cents(11) == 9 * 900 + 1 * 800  # 8900


def test_bruce_seven_arrays():
    # The live pilot: 7 arrays = 1 free + 6 @ $9 = $54/mo
    assert compute_monthly_cents(7) == 6 * 900  # 5400


def test_fleet_band_50_and_51():
    # 1 free + 9@$9 (=8100) + 40@$8 (=32000) = 40100 at 50 arrays
    assert compute_monthly_cents(50) == 9 * 900 + 40 * 800  # 40100
    # 51st array enters the $6.50 fleet band
    assert compute_monthly_cents(51) == 40100 + 1 * 650  # 40750


def test_fleet_floor_is_marginal_650():
    big = compute_monthly_cents(10_000)
    prev = compute_monthly_cents(9_999)
    assert big - prev == 650  # each marginal array past 50 costs exactly $6.50


def test_blended_unit_average():
    assert blended_unit_cents(0) == FULL_UNIT_CENTS
    assert blended_unit_cents(1) == 0            # single free array → $0 average
    assert blended_unit_cents(10) == round(8100 / 10)  # 810


def test_stripe_tiers_shape_matches_table():
    assert stripe_tiers() == [
        {"up_to": 1, "unit_amount": 0},
        {"up_to": 10, "unit_amount": 900},
        {"up_to": 50, "unit_amount": 800},
        {"up_to": "inf", "unit_amount": 650},
    ]
