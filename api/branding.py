"""Product-aware branding: map a tenant's product to its brand name + the
public dashboard/marketing URLs used in transactional emails and Stripe
return URLs.

Two products share ONE backend (see models.Tenant.product):
  - "nepool"          → NEPOOL Operator  (nepooloperator.com)   [default]
  - "array_operator"  → Array Operator   (arrayoperator.com)

IMPORTANT — safe-by-default: arrayoperator.com is not necessarily live/proxied
yet. So AO_APP_URL DEFAULTS to the NEPOOL domain, which already proxies
/accounts + /onboarding to Railway and therefore yields a WORKING magic-link
today. The moment arrayoperator.com resolves + proxies, flip it with a single
env var (AO_APP_URL=https://arrayoperator.com) — no code change, no redeploy of
logic. Never emit a dead-link domain just because the brand exists on paper.
"""
from __future__ import annotations

import os

# NEPOOL (default product). Mirrors APP_URL used elsewhere; kept here so all
# brand/url resolution flows through one module.
_NEPOOL_APP_URL = os.getenv("APP_URL", "https://nepooloperator.com").rstrip("/")

# Array Operator. DEFAULTS to the NEPOOL domain (which works today) — override
# with AO_APP_URL once arrayoperator.com is live and proxying /accounts.
_AO_APP_URL = os.getenv("AO_APP_URL", _NEPOOL_APP_URL).rstrip("/")

_BRANDS = {
    "nepool": {
        "name": "NEPOOL Operator",
        "app_url": _NEPOOL_APP_URL,
    },
    "array_operator": {
        "name": "Array Operator",
        "app_url": _AO_APP_URL,
    },
}


def _key(product: str | None) -> str:
    return "array_operator" if (product or "nepool") == "array_operator" else "nepool"


def brand_name(product: str | None) -> str:
    """Human brand name for emails/UI, e.g. 'NEPOOL Operator' / 'Array Operator'."""
    return _BRANDS[_key(product)]["name"]


def app_url(product: str | None) -> str:
    """Marketing/app origin for this product, no trailing slash."""
    return _BRANDS[_key(product)]["app_url"]


def dashboard_url(product: str | None) -> str:
    """Public buyer-facing dashboard URL (…/accounts), no trailing slash.

    Honors PUBLIC_DASHBOARD_URL for the NEPOOL default (back-compat with the
    existing override) so this never regresses current behavior.
    """
    if _key(product) == "nepool":
        return os.getenv(
            "PUBLIC_DASHBOARD_URL", f"{_NEPOOL_APP_URL}/accounts"
        ).rstrip("/")
    return f"{_AO_APP_URL}/accounts"
