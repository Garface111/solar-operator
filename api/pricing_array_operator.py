"""Single source of truth for ARRAY OPERATOR (owner-side) per-array pricing.

This is the EnergyAgent "Array Operator" product — the owner-facing app, a
DIFFERENT product from the NEPOOL Operator verifier side (see api/pricing.py).
The two share one backend engine but are priced for two different buyers:

  - NEPOOL Operator (api/pricing.py): verifier replacing a ~$540/array/yr human
    consultant → $15/array/mo graduated, $250 one-time setup.
  - Array Operator (this file): the array OWNER. A single residential rooftop
    owner leaks maybe $100-300/yr; the monitoring fee must stay BELOW the loss
    it catches or the value math doesn't close. So the owner price is lower, the
    setup fee is dropped, and the first array is free to make the residential
    top-of-funnel frictionless (the dashboard becomes the sales pitch).

Pricing shape (Option B, Jun 2026 — DEFAULT pending Ford's final sign-off):
  first array     FREE      (1 free array forever — residential wedge)
  arrays 2-10     $9.00     (full owner unit)
  arrays 11-50    $8.00     (~11% off — prosumer / small commercial)
  arrays 51+      $6.50     (~28% off — community-solar host like Bruce's fleet)

Graduated/tiered exactly like api/pricing.py: each band's unit price applies
only to the arrays within that band, so there is never a revenue cliff. The
"first array free" is modeled as a $0 first tier (up_to=1, unit=0) — Stripe
handles it natively under tiers_mode="graduated".

To change pricing: edit TIERS here ONLY, then re-run
scripts/create_array_operator_prices.py to mint a new Stripe price and update
STRIPE_AO_ARRAY_PRICE_ID. Nothing else hardcodes the numbers.
"""
from __future__ import annotations

# Each tier: (up_to, unit_amount_cents). `up_to` is the inclusive array count at
# which this tier's unit price stops applying; the final tier uses None for
# "infinity". Ordered ascending. Mirrors Stripe `tiers` with
# tiers_mode="graduated".
TIERS: list[tuple[int | None, int]] = [
    (1,    0),     # 1st array      FREE          (residential wedge)
    (10,   900),   # arrays 2-10    @ $9.00       (full owner unit)
    (50,   800),   # arrays 11-50   @ $8.00       (~11% off)
    (None, 650),   # arrays 51+     @ $6.50       (~28% off — fleet host)
]

# The headline per-array price an owner sees ("$9/array/mo"): the first PAID
# tier, not the $0 free tier.
FULL_UNIT_CENTS = TIERS[1][1]  # $9.00

# No one-time setup fee on the owner side (NEPOOL has $250; owners don't).
SETUP_CENTS = 0


def compute_monthly_cents(array_count: int) -> int:
    """Total monthly cents for `array_count` arrays under graduated tiers.

    Mirrors Stripe tiers_mode="graduated": each tier's unit price applies only
    to the arrays that fall within that tier's band. Returns 0 for count <= 0
    AND for a single array (the first is free). The Stripe subscription enforces
    its own quantity>=1 minimum separately — this is a pure pricing function.
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
