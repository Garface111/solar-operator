"""Single source of truth for the ARRAY OPERATOR *invoicing* plan.

Array Operator bills two different jobs on two different meters:

  - **Monitoring** (api/pricing_array_operator.py): per-kWh METERED — value scales
    with generation (catching lost production, warranty money). The AO default.
  - **Invoicing** (THIS file): per-OFFTAKER LICENSED — the operator uses AO to
    auto-generate + send offtaker invoices. Value scales with how many billing
    relationships they run, not how much they generate.

Pricing shape (Ford, Jun 2026): a **flat $20.00/mo per offtaker**, no base/floor.
  monthly  = offtakers × $20.00
  setup    = $250.00 one-time (waivable per-customer; not part of the monthly bill)

  1 offtaker → $20/mo · 4 → $80 · 10 → $200 · 20 → $400

On the "both" plan the operator pays this offtaker line PLUS the per-kWh monitoring
meter; the bill shows them itemized (generation × rate, offtakers × $20, summed).

Stripe: a flat LICENSED price (unit_amount per offtaker, quantity = offtaker count,
kept in sync by stripe_helpers.reconcile). To change pricing edit the constants
here ONLY, then re-run scripts/create_ao_invoicing_price.py and update
STRIPE_AO_INVOICING_PRICE_ID / STRIPE_AO_INVOICING_SETUP_PRICE_ID.
"""
from __future__ import annotations

# Whole cents (this plan is in whole dollars → plain integer unit_amount).
PER_OFFTAKER_CENTS: int = 2_000   # $20.00/mo per offtaker (flat — no base, no floor)
SETUP_CENTS: int = 25_000         # $250.00 one-time setup (waivable per-customer)

# Back-compat shims for older callers (no base/floor in the flat model).
BASE_CENTS: int = 0
BASE_INCLUDES_OFFTAKERS: int = 0


def compute_monthly_cents(offtakers: int | None) -> int:
    """Total monthly cents = offtakers × PER_OFFTAKER_CENTS. 0 for offtakers <= 0.

      0 -> 0    1 -> 2000 ($20)    4 -> 8000 ($80)    20 -> 40000 ($400)
    """
    n = int(offtakers or 0)
    return n * PER_OFFTAKER_CENTS if n > 0 else 0


def stripe_tiers() -> list[dict]:
    """Flat per-offtaker price as a single Stripe `tiers` entry (every offtaker at
    the same unit_amount). A flat price doesn't strictly need tiers, but returning
    one keeps the mint script's tiered-price path uniform across plans."""
    return [{"up_to": "inf", "unit_amount": PER_OFFTAKER_CENTS}]
