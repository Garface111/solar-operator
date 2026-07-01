"""Tests for the Array Operator MONITORING plan's per-kW NAMEPLATE volume
discount (Ford, Jun 2026).

Pins the kW tier boundaries so a future edit can't silently change what a
monitoring customer is billed. These numbers must match the live Stripe
graduated LICENSED price built by scripts/create_ao_nameplate_tiered_price.py.

Tiers mirror NEPOOL's 0/10/20/30% discount curve, re-keyed to kW: 0-1,000 @
$0.150/kW, 1,000-5,000 @ $0.135/kW (10% off), 5,000-20,000 @ $0.120/kW (20% off
— mid AO customer), 20,000+ @ $0.105/kW (30% off — regional O&M).
"""
import math

from api.pricing_ao_nameplate import (
    compute_monthly_cents,
    blended_unit_cents,
    stripe_tiers,
    TIERS,
    FULL_UNIT_CENTS,
    SETUP_CENTS,
)


def test_constants():
    assert FULL_UNIT_CENTS == 15.0   # $0.150/kW headline (first tier)
    assert SETUP_CENTS == 0
    assert TIERS[-1][0] is None      # last tier is open-ended


def test_zero_or_none_is_free():
    assert compute_monthly_cents(0) == 0.0
    assert compute_monthly_cents(None) == 0.0
    assert compute_monthly_cents(-5) == 0.0


def test_within_first_tier_matches_old_flat_rate():
    # Bruce's live 983 kW stays fully inside the 0-1,000 band → the migration
    # to graduated tiers must NOT change what he's charged today.
    assert compute_monthly_cents(983) == 983 * 15.0
    assert compute_monthly_cents(1) == 15.0
    assert compute_monthly_cents(1_000) == 1_000 * 15.0   # exactly at the boundary


def test_graduated_across_tiers_no_cliff():
    # 1,001st kW crosses into the 10%-off band: 1,000 @ 15.0 + 1 @ 13.5.
    assert compute_monthly_cents(1_001) == 1_000 * 15.0 + 13.5
    # 5,000 kW: 1,000 @ 15.0 + 4,000 @ 13.5 = 15,000 + 54,000 = 69,000¢.
    assert compute_monthly_cents(5_000) == 1_000 * 15.0 + 4_000 * 13.5
    # 5,001st kW crosses into the 20%-off band: + 1 @ 12.0.
    assert compute_monthly_cents(5_001) == 1_000 * 15.0 + 4_000 * 13.5 + 12.0
    # 20,000 kW: 1,000@15.0 + 4,000@13.5 + 15,000@12.0 = 15,000+54,000+180,000 = 249,000¢.
    assert compute_monthly_cents(20_000) == 1_000 * 15.0 + 4_000 * 13.5 + 15_000 * 12.0
    # 20,001st kW crosses into the 30%-off band: + 1 @ 10.5.
    base_at_20k = 1_000 * 15.0 + 4_000 * 13.5 + 15_000 * 12.0
    assert compute_monthly_cents(20_001) == base_at_20k + 10.5
    # A big regional-O&M fleet, e.g. 100,000 kW: base_at_20k + 80,000 @ 10.5.
    assert compute_monthly_cents(100_000) == base_at_20k + 80_000 * 10.5


def test_blended_rate_reproduces_total():
    for kw in (1, 983, 1_000, 1_001, 5_000, 5_001, 20_000, 20_001, 100_000):
        total = compute_monthly_cents(kw)
        blended = blended_unit_cents(kw)
        assert math.isclose(blended * kw, total, rel_tol=1e-9), (kw, blended, total)
    # No kW registered yet → headline rate, not a divide-by-zero.
    assert blended_unit_cents(0) == FULL_UNIT_CENTS


def test_no_revenue_cliff_at_breakpoints():
    for kw in (999, 1_000, 1_001, 4_999, 5_000, 5_001, 19_999, 20_000, 20_001):
        assert compute_monthly_cents(kw + 1) > compute_monthly_cents(kw)


def test_stripe_tiers_shape():
    assert stripe_tiers() == [
        {"up_to": 1_000, "unit_amount_decimal": "15"},
        {"up_to": 5_000, "unit_amount_decimal": "13.5"},
        {"up_to": 20_000, "unit_amount_decimal": "12"},
        {"up_to": "inf", "unit_amount_decimal": "10.5"},
    ]
