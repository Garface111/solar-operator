"""VT electricity + REC value model for the Array Owners (EnergyAgent) view.

The dashboard converts raw generation into a dollar figure two ways:
  - energy offset: kWh × retail rate (the bill the owner avoids)
  - REC value:     MWh × the REC market price (what the certificate sells for)

Rates are keyed by the array's utility provider when known; otherwise we fall
back to a VT blended residential default. See ARRAY_OWNERS_API_CONTRACT.md.
"""
from __future__ import annotations

import os

# Retail offset rate ($/kWh) by provider code (UtilityAccount.provider).
# VT-specific, rough residential blended rates — refined as we learn each
# utility's actual net-metering credit value.
VT_RATES: dict[str, float] = {
    "gmp": 0.21,
    "vec": 0.22,
    "wec": 0.23,
    "stowe": 0.20,
}

# Used when the provider is unknown or not in the table (VT blended residential).
DEFAULT_RATE_USD_PER_KWH: float = 0.21

# REC market price ($/MWh). Overridable via env so ops can track the market
# without a deploy. Default is a conservative VT solar REC clearing price.
REC_PRICE_USD_PER_MWH: float = float(os.environ.get("REC_PRICE_USD_PER_MWH", "35.0"))


def get_energy_rate(provider: str | None) -> float:
    """Return the retail offset rate ($/kWh) for a utility provider code.

    Unknown / missing providers fall back to DEFAULT_RATE_USD_PER_KWH. Matching
    is case-insensitive and whitespace-tolerant since provider codes arrive from
    captured portal data.
    """
    if not provider:
        return DEFAULT_RATE_USD_PER_KWH
    return VT_RATES.get(provider.strip().lower(), DEFAULT_RATE_USD_PER_KWH)
