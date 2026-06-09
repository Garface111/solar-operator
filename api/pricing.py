"""Single source of truth for Solar Operator per-array volume pricing.

Billing is graduated/tiered: each band of arrays is priced on the arrays that
fall *within* that band (the way AWS / Stripe / Twilio volume pricing works),
NOT a single whole-bill discount. Graduated tiers can never cross the floor, so
there is no revenue cliff.

The SAME table feeds two places that must agree to the penny:
  1. The live Stripe Price (scripts/create_stripe_prices.py builds a Stripe
     graduated tiered price from TIERS). Stripe is the source of truth for the
     actual charge — it applies these tiers automatically given the quantity.
  2. The dashboard billing estimate (api/account.billing_summary) calls
     compute_monthly_cents() so the operator's "next charge" preview matches the
     Stripe invoice exactly.

Pricing shape (Ford, Jun 9 '26): "10% off for every 50 past 50", 30% cap.
  arrays 1–50     $15.00   (0% off — full)
  arrays 51–100   $13.50   (10% off)
  arrays 101–150  $12.00   (20% off)
  arrays 151+     $10.50   (30% off — floor/cap)

To change pricing, edit TIERS here ONLY, then re-run create_stripe_prices.py to
mint a new Stripe price and update STRIPE_ARRAY_PRICE_ID. Nothing else hardcodes
the numbers.
"""
from __future__ import annotations

# Each tier: (up_to, unit_amount_cents). `up_to` is the inclusive array count at
# which this tier's unit price stops applying; the final tier uses None to mean
# "infinity" (every array beyond the prior boundary). Ordered ascending.
# This mirrors Stripe's `tiers` array with tiers_mode="graduated".
TIERS: list[tuple[int | None, int]] = [
    (50,   1500),  # arrays 1–50    @ $15.00  (0% off)
    (100,  1350),  # arrays 51–100  @ $13.50  (10% off)
    (150,  1200),  # arrays 101–150 @ $12.00  (20% off)
    (None, 1050),  # arrays 151+    @ $10.50  (30% off — cap)
]

FULL_UNIT_CENTS = TIERS[0][1]  # $15.00 — the undiscounted per-array price


def compute_monthly_cents(array_count: int) -> int:
    """Total monthly cents for `array_count` arrays under graduated tiers.

    Mirrors Stripe tiers_mode="graduated": each tier's unit price applies only to
    the arrays that fall within that tier's band. Returns 0 for a count <= 0
    (comped/zero-array accounts); the Stripe subscription enforces its own
    quantity>=1 minimum separately — this is a pure pricing function.
    """
    if array_count <= 0:
        return 0
    remaining = array_count
    prev_bound = 0
    total = 0
    for up_to, unit in TIERS:
        if up_to is None:
            total += remaining * unit
            remaining = 0
            break
        band = up_to - prev_bound
        take = min(remaining, band)
        total += take * unit
        remaining -= take
        prev_bound = up_to
        if remaining <= 0:
            break
    return total


def blended_unit_cents(array_count: int) -> int:
    """Average per-array cents (total / count), rounded. For display only."""
    if array_count <= 0:
        return FULL_UNIT_CENTS
    return round(compute_monthly_cents(array_count) / array_count)


def stripe_tiers() -> list[dict]:
    """TIERS as Stripe Price `tiers` payload (tiers_mode='graduated')."""
    out: list[dict] = []
    for up_to, unit in TIERS:
        out.append({"up_to": "inf" if up_to is None else up_to,
                    "unit_amount": unit})
    return out
