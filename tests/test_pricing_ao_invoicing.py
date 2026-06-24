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
from api.stripe_helpers import is_ao_invoicing, is_array_operator


def test_constants():
    assert BASE_CENTS == 10_000           # $100/mo base
    assert BASE_INCLUDES_OFFTAKERS == 4
    assert PER_OFFTAKER_CENTS == 2_500    # $25/offtaker beyond
    assert SETUP_CENTS == 25_000          # $250 one-time setup


def test_zero_is_free():
    assert compute_monthly_cents(0) == 0
    assert compute_monthly_cents(None) == 0
    assert compute_monthly_cents(-3) == 0


def test_floor_covers_up_to_four():
    # 1..4 offtakers all cost the base ($100) — the floor.
    for n in (1, 2, 3, 4):
        assert compute_monthly_cents(n) == 10_000, f"{n} offtakers should be $100"


def test_pauls_anchor():
    # Paul: 4 offtakers = $100/mo (the WTP signal this plan is built on).
    assert compute_monthly_cents(4) == 10_000


def test_per_offtaker_beyond_base():
    assert compute_monthly_cents(5) == 12_500    # $125
    assert compute_monthly_cents(6) == 15_000    # $150
    assert compute_monthly_cents(10) == 25_000   # $250
    assert compute_monthly_cents(20) == 50_000   # $500


def test_stripe_tiers_shape():
    tiers = stripe_tiers()
    assert tiers == [
        {"up_to": 4, "flat_amount": 10_000, "unit_amount": 0},
        {"up_to": "inf", "unit_amount": 2_500},
    ]


def test_is_ao_invoicing_detection():
    # Only an array_operator tenant explicitly on the invoicing plan.
    assert is_ao_invoicing("array_operator", "invoicing") is True
    assert is_ao_invoicing("array_operator", "INVOICING") is True   # case-insensitive
    assert is_ao_invoicing("array_operator", " invoicing ") is True  # trimmed
    # AO default (per-kWh meter) — NOT invoicing.
    assert is_ao_invoicing("array_operator", None) is False
    assert is_ao_invoicing("array_operator", "") is False
    assert is_ao_invoicing("array_operator", "kwh") is False
    # NEPOOL is never invoicing, regardless of billing_plan.
    assert is_ao_invoicing("nepool", "invoicing") is False
    assert is_ao_invoicing(None, "invoicing") is False
    # Sanity: invoicing tenants are still array_operator tenants.
    assert is_array_operator("array_operator") is True
