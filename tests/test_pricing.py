"""Tests for graduated volume pricing (api/pricing.py).

Pins the tier boundaries so a future edit to TIERS can't silently change what
operators are billed without a test failure. These numbers must match the live
Stripe graduated price built by scripts/create_stripe_prices.py.
"""
from api.pricing import (
    compute_monthly_cents,
    blended_unit_cents,
    stripe_tiers,
    FULL_UNIT_CENTS,
)


def test_zero_and_negative_are_free():
    assert compute_monthly_cents(0) == 0
    assert compute_monthly_cents(-5) == 0


def test_full_rate_within_first_tier():
    assert FULL_UNIT_CENTS == 1500
    assert compute_monthly_cents(1) == 1500
    assert compute_monthly_cents(50) == 50 * 1500  # 75000


def test_first_boundary_crossing():
    # 50 @ $15 + 1 @ $13.50
    assert compute_monthly_cents(51) == 50 * 1500 + 1 * 1350  # 76350


def test_second_tier_full():
    # 50 @ $15 + 50 @ $13.50
    assert compute_monthly_cents(100) == 50 * 1500 + 50 * 1350  # 142500


def test_third_tier_boundary():
    # 50@15 + 50@13.5 + 1@12
    assert compute_monthly_cents(101) == 50 * 1500 + 50 * 1350 + 1 * 1200  # 143700
    # 50@15 + 50@13.5 + 50@12
    assert compute_monthly_cents(150) == 50 * 1500 + 50 * 1350 + 50 * 1200  # 202500


def test_floor_tier_caps_at_30pct():
    # 151st array is at the floor $10.50 (30% off $15)
    assert compute_monthly_cents(151) == 202500 + 1 * 1050  # 203550
    # Floor unit never drops below $10.50 no matter how large
    big = compute_monthly_cents(10_000)
    prev = compute_monthly_cents(9_999)
    assert big - prev == 1050  # each marginal array past 150 costs exactly $10.50


def test_worked_example_120_arrays():
    # The number quoted to the operator: 120 arrays = $1,665/mo
    assert compute_monthly_cents(120) == 166500


def test_blended_unit_is_average():
    assert blended_unit_cents(0) == FULL_UNIT_CENTS
    assert blended_unit_cents(50) == 1500
    assert blended_unit_cents(120) == round(166500 / 120)  # 1388


def test_blended_never_below_floor():
    # Even at huge counts the blended rate asymptotes toward but stays >= $10.50
    assert blended_unit_cents(100_000) >= 1050


def test_stripe_tiers_shape_matches_table():
    tiers = stripe_tiers()
    assert tiers == [
        {"up_to": 50, "unit_amount": 1500},
        {"up_to": 100, "unit_amount": 1350},
        {"up_to": 150, "unit_amount": 1200},
        {"up_to": "inf", "unit_amount": 1050},
    ]
