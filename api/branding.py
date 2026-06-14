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

# Array Operator. arrayoperator.com is live + proxying, so it is the default now
# (no env flip needed). AO_APP_URL can still override for staging/preview.
_AO_APP_URL = os.getenv("AO_APP_URL", "https://arrayoperator.com").rstrip("/")

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
    """Public buyer-facing dashboard URL, no trailing slash.

    PER-PRODUCT, because the two products serve their owner dashboard at
    DIFFERENT paths:
      - NEPOOL Operator → nepooloperator.com/accounts (the React SPA, honoring
        the PUBLIC_DASHBOARD_URL override for back-compat).
      - Array Operator  → arrayoperator.com  (the owner dashboard is the SITE
        ROOT / app.js). Its /accounts path proxies to the NEPOOL verifier SPA,
        so /accounts is WRONG for an array owner.
    """
    if _key(product) == "nepool":
        return os.getenv(
            "PUBLIC_DASHBOARD_URL", f"{_NEPOOL_APP_URL}/accounts"
        ).rstrip("/")
    return _AO_APP_URL


def magic_link_url(product: str | None, token: str) -> str:
    """Where a one-time sign-in token should LAND for this product — a page that
    exchanges it via POST /v1/auth/verify.

      - NEPOOL → /accounts/?token=…  (the SPA AuthGate verifies + shows errors).
      - Array Operator → /login?token=…  (login.html verifies, shows errors, and
        redirects into the owner dashboard on success).

    Product-correct by the TENANT's product, so whichever login page the email
    was requested from, the link always lands on the owner's real brand.
    """
    if _key(product) == "nepool":
        return f"{dashboard_url('nepool')}/?token={token}"
    return f"{_AO_APP_URL}/login?token={token}"
