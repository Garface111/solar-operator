"""Single source of truth for the ARRAY OPERATOR *invoicing* plan.

Array Operator bills two different jobs on two different meters:

  - **Monitoring** (api/pricing_ao_nameplate.py): per-kW NAMEPLATE — value scales
    with registered fleet capacity (catching lost production, warranty money).
    The AO default.
  - **Invoicing** (THIS file): per-OFFTAKER LICENSED — the operator uses AO to
    auto-generate + send offtaker invoices. Value scales with how many billing
    relationships they run, not how much they generate.

Pricing shape (Ford, Jun 2026 — bulk-discounted, replacing the earlier flat
$20/offtaker): a GRADUATED volume discount, mirroring the SAME 0% / 10% / 20% /
30% curve NEPOOL already uses for per-array pricing (api/pricing.py) and AO
monitoring now uses for per-kW pricing (api/pricing_ao_nameplate.py) — just
re-keyed to offtaker count, sized for muni / property-manager-scale portfolios:

  1 – 10 offtakers    $20.00/mo   (full)
  11 – 25 offtakers   $18.00/mo   (10% off)
  26 – 50 offtakers   $16.00/mo   (20% off)
  51+ offtakers       $14.00/mo   (30% off)

  monthly  = graduated sum across bands (each offtaker priced per its own band)
  setup    = $250.00 one-time (waivable per-customer; not part of the monthly bill)

  4 offtakers → $80    12 → $236    30 → $436    60 → $780

On the "both" plan the operator pays this offtaker line PLUS the per-kW
monitoring meter; the bill shows them itemized (generation × rate, offtakers ×
rate, summed).

Stripe: a LICENSED price (billing_scheme='tiered', tiers_mode='graduated',
quantity = offtaker count). To change pricing edit TIERS here ONLY, then mint a
NEW Stripe price (prices are immutable — tiers can't be edited in place) via
scripts/create_ao_invoicing_tiered_price.py, point
STRIPE_AO_INVOICING_PRICE_ID at it, and migrate any live subscription's
invoicing item to the new price id (quantity/mechanism unchanged).
"""
from __future__ import annotations

# Each tier: (up_to_offtakers, unit_amount_cents). `up_to_offtakers` is the
# inclusive offtaker-count boundary at which this tier's unit price stops
# applying; the final tier uses None for "infinity". Ordered ascending. Whole
# cents (this plan is in whole dollars).
TIERS: list[tuple[int | None, int]] = [
    (10, 2_000),   # 1–10 offtakers    @ $20.00/mo  (full)
    (25, 1_800),   # 11–25 offtakers   @ $18.00/mo  (10% off)
    (50, 1_600),   # 26–50 offtakers   @ $16.00/mo  (20% off)
    (None, 1_400), # 51+ offtakers     @ $14.00/mo  (30% off)
]

# The headline per-offtaker price an operator sees ("$20/offtaker"): the first
# (full) tier. Whole cents.
PER_OFFTAKER_CENTS: int = TIERS[0][1]   # $20.00/mo (back-compat name/value)
SETUP_CENTS: int = 25_000               # $250.00 one-time setup (waivable per-customer)

# Back-compat shims for older callers (no base/floor in this model).
BASE_CENTS: int = 0
BASE_INCLUDES_OFFTAKERS: int = 0


def compute_monthly_cents(offtakers: int | None) -> int:
    """Total monthly cents under graduated volume tiers. 0 for offtakers <= 0.

    Mirrors Stripe tiers_mode="graduated": each tier's unit price applies only
    to the offtakers that fall within that tier's band (no revenue cliff).

      0 -> 0    4 -> 8000 ($80)    12 -> 23600 ($236)    60 -> 78000 ($780)
    """
    n = int(offtakers or 0)
    if n <= 0:
        return 0
    remaining = n
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


def blended_unit_cents(offtakers: int | None) -> float:
    """Average per-offtaker cents (total / count). For display only — this is
    what "×" against the raw offtaker count reproduces the real total, unlike
    the flat headline rate once a portfolio has crossed into a discounted tier.

    Returns the full headline rate when offtakers <= 0 (none yet)."""
    n = int(offtakers or 0)
    if n <= 0:
        return float(PER_OFFTAKER_CENTS)
    return compute_monthly_cents(n) / float(n)


def stripe_tiers() -> list[dict]:
    """TIERS as a Stripe Price `tiers` payload (tiers_mode='graduated')."""
    return [{"up_to": "inf" if up_to is None else up_to, "unit_amount": unit}
            for up_to, unit in TIERS]
