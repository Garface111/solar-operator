"""Single source of truth for the ARRAY OPERATOR *invoicing* plan.

Array Operator bills two different jobs on two different meters:

  - **Monitoring** (api/pricing_ao_nameplate.py): per-kW NAMEPLATE — value scales
    with registered fleet capacity (catching lost production, warranty money).
    The AO default.
  - **Invoicing** (THIS file): per-OFFTAKER LICENSED — the operator uses AO to
    auto-generate + send offtaker invoices. Value scales with how many billing
    relationships they run, not how much they generate.

Pricing shape (Ford, Jul 2026 — Path A growth reprice from $20 → $15 headline):
a GRADUATED volume discount, mirroring the SAME 0% / 10% / 20% / 30% curve
NEPOOL uses for per-array pricing and AO monitoring uses for per-kW pricing:

  1 – 10 offtakers    $15.00/mo   (full)
  11 – 25 offtakers   $13.50/mo   (10% off)
  26 – 50 offtakers   $12.00/mo   (20% off)
  51+ offtakers       $10.50/mo   (30% off)

  monthly  = graduated sum across bands (each offtaker priced per its own band)
  setup    = $250.00 one-time (waivable per-customer; not part of the monthly bill)

  4 offtakers → $60    12 → $177    30 → $412.50    60 → $757.50

Separate from the monthly line: when offtakers pay invoices online via Stripe
Connect, the platform keeps a small collection fee (default 0.5% — see
api/billing/payments.py). That fee is NOT a monthly SaaS charge; Account Billing
shows it for transparency only.

On the "both" plan the operator pays this offtaker line PLUS the per-kW
monitoring meter; the bill shows them itemized.

Stripe: a LICENSED price (billing_scheme='tiered', tiers_mode='graduated',
quantity = offtaker count). To change pricing edit TIERS here ONLY, then mint a
NEW Stripe price via scripts/create_ao_invoicing_price.py, point
STRIPE_AO_INVOICING_PRICE_ID at it, and migrate live subscription items
(scripts/migrate_ao_bulk_tiers_live.py --old-invoicing …).
"""
from __future__ import annotations

# Each tier: (up_to_offtakers, unit_amount_cents). `up_to_offtakers` is the
# inclusive offtaker-count boundary at which this tier's unit price stops
# applying; the final tier uses None for "infinity". Ordered ascending. Whole
# cents (this plan is in whole dollars / half-dollars).
TIERS: list[tuple[int | None, int]] = [
    (10, 1_500),   # 1–10 offtakers    @ $15.00/mo  (full)
    (25, 1_350),   # 11–25 offtakers   @ $13.50/mo  (10% off)
    (50, 1_200),   # 26–50 offtakers   @ $12.00/mo  (20% off)
    (None, 1_050), # 51+ offtakers     @ $10.50/mo  (30% off)
]

# The headline per-offtaker price an operator sees ("$15/offtaker"): the first
# (full) tier. Whole cents.
PER_OFFTAKER_CENTS: int = TIERS[0][1]   # $15.00/mo
SETUP_CENTS: int = 25_000               # $250.00 one-time setup (waivable per-customer)

# Back-compat shims for older callers (no base/floor in this model).
BASE_CENTS: int = 0
BASE_INCLUDES_OFFTAKERS: int = 0


def compute_monthly_cents(offtakers: int | None) -> int:
    """Total monthly cents under graduated volume tiers. 0 for offtakers <= 0.

    Mirrors Stripe tiers_mode="graduated": each tier's unit price applies only
    to the offtakers that fall within that tier's band (no revenue cliff).

      0 -> 0    4 -> 6000 ($60)    12 -> 17700 ($177)    60 -> 75750 ($757.50)
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
