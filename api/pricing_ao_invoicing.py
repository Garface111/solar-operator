"""Single source of truth for the ARRAY OPERATOR *invoicing* plan.

Array Operator bills two different jobs on two different meters:

  - **Monitoring** (api/pricing_array_operator.py): per-kWh METERED — value scales
    with generation (catching lost production, warranty money). The AO default.
  - **Invoicing** (THIS file): per-OFFTAKER LICENSED — the operator uses AO only to
    auto-generate + send offtaker invoices. Value scales with how many billing
    relationships they run, not how much they generate. Anchored on a real WTP
    signal: Paul Bozuwa, 4 offtakers, said **$100/mo is reasonable** (= $25/offtaker).

Pricing shape (Ford sign-off Jun 2026):
  base                 $100.00/mo   includes up to 4 offtakers (the floor)
  each offtaker > 4    $25.00/mo    per-offtaker beyond the base
  one-time setup       $250.00      (configuring offtakers, templates, GMP links;
                                     waivable per-customer — Paul is grandfathered)

  Paul (4 offtakers)   → $100/mo     ·  5 → $125  ·  10 → $250

This mirrors the NEPOOL per-array LICENSED plan (api/pricing.py): a Stripe price
with tiers_mode="graduated", subscription quantity = the OFFTAKER count, kept in
sync by stripe_helpers.reconcile, plus a one-time $250 setup attached to the first
invoice. Unlike monitoring it is NOT metered — there is a real quantity to bill.

To change pricing: edit the constants here ONLY, then re-run
scripts/create_ao_invoicing_price.py to mint a new Stripe price + setup price and
update STRIPE_AO_INVOICING_PRICE_ID / STRIPE_AO_INVOICING_SETUP_PRICE_ID. Nothing
else hardcodes the numbers.
"""
from __future__ import annotations

# Whole cents (this plan is in whole dollars, so plain integer `unit_amount` —
# unlike the sub-cent per-kWh meter which needs unit_amount_decimal).
BASE_CENTS: int = 10_000          # $100.00/mo base
BASE_INCLUDES_OFFTAKERS: int = 4  # the base covers up to this many offtakers
PER_OFFTAKER_CENTS: int = 2_500   # $25.00/mo per offtaker beyond the base
SETUP_CENTS: int = 25_000         # $250.00 one-time setup (waivable per-customer)


def compute_monthly_cents(offtakers: int | None) -> int:
    """Total monthly cents for `offtakers` billing relationships, graduated.

    Mirrors Stripe tiers_mode="graduated" with the tiers from `stripe_tiers()`:
    the base (tier 1 flat_amount) is charged once for any quantity >= 1, then each
    offtaker beyond BASE_INCLUDES_OFFTAKERS adds PER_OFFTAKER_CENTS. Returns 0 for
    offtakers <= 0 (no billable relationships).

      0  -> 0       1..4 -> 10000 ($100)     5 -> 12500 ($125)     10 -> 25000 ($250)
    """
    n = int(offtakers or 0)
    if n <= 0:
        return 0
    total = BASE_CENTS
    if n > BASE_INCLUDES_OFFTAKERS:
        total += (n - BASE_INCLUDES_OFFTAKERS) * PER_OFFTAKER_CENTS
    return total


def stripe_tiers() -> list[dict]:
    """The plan as a Stripe Price `tiers` payload (tiers_mode='graduated').

    Tier 1 (up_to = BASE_INCLUDES_OFFTAKERS): flat_amount = the base, unit_amount 0
    — so any quantity 1..4 costs exactly the base. Tier 2 (up_to = inf): unit_amount
    = the per-offtaker price for each offtaker beyond the base. Whole-cent integers.
    """
    return [
        {"up_to": BASE_INCLUDES_OFFTAKERS, "flat_amount": BASE_CENTS, "unit_amount": 0},
        {"up_to": "inf", "unit_amount": PER_OFFTAKER_CENTS},
    ]
