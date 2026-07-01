"""Single source of truth for ARRAY OPERATOR monitoring's per-kW NAMEPLATE
pricing, INCLUDING the volume/bulk discount (Ford, Jun 2026).

Monitoring bills on REGISTERED INVERTER NAMEPLATE (kW) — see stripe_helpers for
why (deterministic, immune to vendor-portal capture gaps). This module adds a
GRADUATED bulk discount on top of that flat $0.15/kW headline, mirroring the
same 0% / 10% / 20% / 30% discount curve NEPOOL already uses for per-array
pricing (api/pricing.py) — just re-keyed to kW instead of array count, and
sized to the researched AO market segments (mid 5-20 MW, regional O&M 100 MW+):

  0 – 1,000 kW        $0.150/kW   (full — covers the typical AO customer today)
  1,000 – 5,000 kW    $0.135/kW   (10% off)
  5,000 – 20,000 kW   $0.120/kW   (20% off — "mid" AO customer, 5-20 MW)
  20,000+ kW          $0.105/kW   (30% off — regional O&M / fleet operator)

Graduated/tiered exactly like pricing.py and pricing_array_operator.py: each
band's unit price applies ONLY to the kW within that band, so there is never a
revenue cliff at a breakpoint. The Stripe side is a LICENSED price
(billing_scheme='tiered', tiers_mode='graduated'); the subscription-item
quantity = the tenant's registered nameplate kW (stripe_helpers.
tenant_nameplate_kw), same mechanism as the pre-discount flat price — Stripe
bins the quantity into these bands automatically.

IMPORTANT — sub-cent units. $0.135 and $0.105/kW are HALF-CENT, below whole-
cent granularity, so Stripe's integer `unit_amount` CANNOT represent them. Use
`unit_amount_decimal` (a string of cents, decimals allowed) instead — same
approach as pricing_array_operator.py. All amounts in this module are
therefore DECIMAL CENTS (float), not whole-cent ints.

To change pricing: edit TIERS here ONLY, then mint a NEW Stripe price (prices
are immutable — tiers can't be edited in place) via
scripts/create_ao_nameplate_tiered_price.py, point
STRIPE_AO_NAMEPLATE_PRICE_ID at it, and migrate any live subscription's
nameplate item to the new price id (quantity/mechanism unchanged).
"""
from __future__ import annotations

# Each tier: (up_to_kw, unit_amount_decimal_cents). `up_to_kw` is the inclusive
# monthly nameplate-kW boundary at which this tier's unit price stops applying;
# the final tier uses None for "infinity". Ordered ascending. Unit is DECIMAL
# CENTS per kW-month (15.0 == $0.15).
TIERS: list[tuple[int | None, float]] = [
    (1_000,  15.0),   # 0–1,000 kW          @ $0.150/kW  (full)
    (5_000,  13.5),   # 1,000–5,000 kW      @ $0.135/kW  (10% off)
    (20_000, 12.0),   # 5,000–20,000 kW     @ $0.120/kW  (20% off — mid AO)
    (None,   10.5),   # 20,000+ kW          @ $0.105/kW  (30% off — regional O&M)
]

# The headline per-kW price an owner sees ("$0.15/kW"): the first (full) tier.
# In decimal cents.
FULL_UNIT_CENTS: float = TIERS[0][1]  # $0.150/kW

# No one-time setup fee on the owner side (matches the existing flat price).
SETUP_CENTS = 0


def compute_monthly_cents(kw: float | int | None) -> float:
    """Total monthly cents for `kw` of registered nameplate under graduated
    volume tiers.

    Mirrors Stripe tiers_mode="graduated": each tier's unit price applies only
    to the kW that fall within that tier's band. Returns 0.0 for kw <= 0.

    Returns DECIMAL cents (a float — may be fractional, e.g. 1 kW × $0.15 =
    15.0¢). Callers that need whole cents for display should round.
    """
    if kw is None or kw <= 0:
        return 0.0
    remaining = float(kw)
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


def blended_unit_cents(kw: float | int | None) -> float:
    """Average per-kW decimal-cents (total / kw). For display only — this is
    what "×" against the raw kW reproduces the real total, unlike the flat
    headline rate once a fleet has crossed into a discounted tier.

    Returns the full headline rate when kw <= 0 (nothing registered yet).
    """
    if kw is None or kw <= 0:
        return FULL_UNIT_CENTS
    return compute_monthly_cents(kw) / float(kw)


def stripe_tiers() -> list[dict]:
    """TIERS as a Stripe Price `tiers` payload (tiers_mode='graduated').

    Emits `unit_amount_decimal` (a STRING of cents) rather than the integer
    `unit_amount`, because two of the bands are sub-cent/half-cent. Stripe
    accepts up to 12 decimal places on unit_amount_decimal.
    """
    out: list[dict] = []
    for up_to, unit in TIERS:
        out.append({"up_to": "inf" if up_to is None else up_to,
                    "unit_amount_decimal": _fmt_decimal_cents(unit)})
    return out


def _fmt_decimal_cents(cents: float) -> str:
    """Format decimal cents for Stripe `unit_amount_decimal` (string, no
    trailing-zero noise): 15.0 -> '15', 13.5 -> '13.5', 10.5 -> '10.5'."""
    s = f"{cents:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"
