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


def _valid_from(value: str | None) -> str | None:
    """Return value only if it looks like a Resend-acceptable From header.

    Resend requires a real address (with @). A bare display name like
    ``NEPOOL Operator`` (which once landed in MAIL_FROM by mistake) makes
    every transactional email fail silently — magic links never arrive.
    """
    v = (value or "").strip()
    if not v or "@" not in v:
        return None
    return v


def from_address(product: str | None) -> str:
    """Product-correct email From header (display name + sender).

    Resend only sends from VERIFIED domains. As of 2026-06-18:
      - nepooloperator.com IS verified → NEPOOL sends from its own domain.
      - arrayoperator.com IS verified → Array Operator sends from its own domain
        (confirmed via the Resend /domains API). Override with MAIL_FROM_AO.
    Replies are routed to the monitored inbox via reply_to_address() regardless
    of the From domain, so changing the From never strands a customer reply.

    Env values that are display-name-only (no @) are ignored so a bad Railway
    var can never black-hole magic links again.
    """
    if _key(product) == "array_operator":
        return (
            _valid_from(os.getenv("MAIL_FROM_AO"))
            or "Array Operator <hello@arrayoperator.com>"
        )
    return (
        _valid_from(os.getenv("MAIL_FROM_NEPOOL"))
        or _valid_from(os.getenv("MAIL_FROM"))
        or "NEPOOL Operator <admin@nepooloperator.com>"
    )


def reply_to_address(product: str | None = None) -> str:
    """The monitored support inbox replies should reach, whatever the From is."""
    return os.getenv("SUPPORT_EMAIL", "admin@solaroperator.org")


def pricing_blurb(product: str | None) -> str:
    """One-sentence, brand-correct pricing phrase for email copy.

    Single source so every lifecycle email quotes the SAME numbers as the
    billing engine (api/pricing.py, api/pricing_array_operator.py)."""
    if _key(product) == "array_operator":
        return ("just 0.5¢ per kWh your arrays generate — no setup fee and no "
                "per-panel charge, so you only ever pay in proportion to what they make "
                "(a typical home array is about $4–$5/month)")
    return "$250 one-time setup plus $15/array/month"
