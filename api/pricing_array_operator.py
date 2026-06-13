"""Single source of truth for ARRAY OPERATOR (owner-side) per-kWh pricing.

This is the EnergyAgent "Array Operator" product — the owner-facing app, a
DIFFERENT product from the NEPOOL Operator verifier side (see api/pricing.py).
The two share one backend engine but are billed on two different meters:

  - NEPOOL Operator (api/pricing.py): a VERIFIER replacing a ~$540/array/yr
    human consultant → billed PER ARRAY ($15/array/mo graduated, $250 setup),
    because each array is one REC-bearing asset the verifier stamps.
  - Array Operator (this file): the array OWNER. Billed PER kWh GENERATED,
    because the value we deliver (catching lost production, warranty money,
    REC eligibility) scales with how much energy the fleet actually makes —
    "directly tied to how much they get paid." A tiny rooftop pays pennies; a
    community-solar host pays in proportion to the revenue we protect.

Pricing shape (Ford sign-off Jun 2026 — 0.5¢/kWh headline, graduated):
  0 – 20,000 kWh/mo       0.50¢/kWh   (full)
  20,000 – 200,000 kWh    0.45¢/kWh   (10% off — prosumer / small commercial)
  200,000+ kWh/mo         0.40¢/kWh   (20% off — community-solar host / fleet)

For reference at the headline 0.5¢ rate:
  residential ~900 kWh/mo            → ~$4.50/mo
  commercial 99 kW ~10,000 kWh/mo    → ~$50.00/mo
  large host 250,000 kWh/mo          → ~$1,200/mo (with the graduated breaks)

Graduated/tiered exactly like api/pricing.py: each band's unit price applies
ONLY to the kWh within that band, so there is never a revenue cliff. The Stripe
side is a METERED price (usage_type='metered', aggregate_usage='last_during_period')
— a usage-reporting job (api/jobs/usage_report.py) sums each tenant's monthly
kWh from DailyGeneration and reports it to Stripe, which applies these tiers.

IMPORTANT — sub-cent units. 0.5¢/kWh is BELOW one cent, so Stripe's integer
`unit_amount` (whole cents) CANNOT represent it. We use `unit_amount_decimal`
(a string of cents, decimals allowed) instead. All amounts in this module are
therefore DECIMAL CENTS (float), not whole-cent ints like pricing.py.

To change pricing: edit TIERS here ONLY, then re-run
scripts/create_array_operator_prices.py to mint a new metered Stripe price and
update STRIPE_AO_KWH_PRICE_ID. Nothing else hardcodes the numbers.
"""
from __future__ import annotations

# Each tier: (up_to_kwh, unit_amount_decimal_cents). `up_to_kwh` is the inclusive
# monthly-kWh boundary at which this tier's unit price stops applying; the final
# tier uses None for "infinity". Ordered ascending. Mirrors Stripe `tiers` with
# tiers_mode="graduated". Unit is DECIMAL CENTS per kWh (0.5 == half a cent).
TIERS: list[tuple[int | None, float]] = [
    (20_000,  0.50),   # 0–20,000 kWh/mo     @ 0.50¢/kWh  (full)
    (200_000, 0.45),   # 20k–200k kWh/mo     @ 0.45¢/kWh  (10% off)
    (None,    0.40),   # 200,000+ kWh/mo     @ 0.40¢/kWh  (20% off — fleet host)
]

# The headline per-kWh price an owner sees ("0.5¢/kWh"): the first (full) tier.
# In decimal cents.
FULL_UNIT_CENTS: float = TIERS[0][1]  # 0.50¢/kWh

# No one-time setup fee on the owner side (NEPOOL has $250; owners don't).
SETUP_CENTS = 0


def compute_monthly_cents(kwh: float | int | None) -> float:
    """Total monthly cents for `kwh` of generation under graduated tiers.

    Mirrors Stripe tiers_mode="graduated": each tier's unit price applies only
    to the kWh that fall within that tier's band. Returns 0.0 for kwh <= 0.

    Returns DECIMAL cents (a float — may be fractional, e.g. 900 kWh × 0.5¢ =
    450.0¢ = $4.50). Callers that need whole cents for display should round.
    """
    if kwh is None or kwh <= 0:
        return 0.0
    remaining = float(kwh)
    prev_bound = 0
    total = 0.0
    for up_to, unit in TIERS:
        if up_to is None:
            total += remaining * unit
            remaining = 0.0
            break
        band = up_to - prev_bound
        take = min(remaining, band)
        total += take * unit
        remaining -= take
        prev_bound = up_to
        if remaining <= 0:
            break
    return total


def blended_unit_cents(kwh: float | int | None) -> float:
    """Average per-kWh decimal-cents (total / kwh). For display only.

    Returns the full headline rate when kwh <= 0 (nothing generated yet).
    """
    if kwh is None or kwh <= 0:
        return FULL_UNIT_CENTS
    return compute_monthly_cents(kwh) / float(kwh)


def stripe_tiers() -> list[dict]:
    """TIERS as a Stripe Price `tiers` payload (tiers_mode='graduated').

    Emits `unit_amount_decimal` (a STRING of cents) rather than the integer
    `unit_amount`, because the per-kWh rate is sub-cent. Stripe accepts up to 12
    decimal places on unit_amount_decimal.
    """
    out: list[dict] = []
    for up_to, unit in TIERS:
        out.append({"up_to": "inf" if up_to is None else up_to,
                    "unit_amount_decimal": _fmt_decimal_cents(unit)})
    return out


def _fmt_decimal_cents(cents: float) -> str:
    """Format decimal cents for Stripe `unit_amount_decimal` (string, no
    trailing-zero noise): 0.5 -> '0.5', 0.45 -> '0.45', 0.4 -> '0.4'."""
    s = f"{cents:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"
