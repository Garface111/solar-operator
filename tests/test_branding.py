"""Tests for product-aware branding (api/branding.py) and its use in the
magic-link + onboarding email URLs.

Contract (post-fold, 2026-07-16): NEPOOL Operator folded into Array Operator
and nepooloperator.com is sunsetting, so BOTH products default to
arrayoperator.com. The NEPOOL surface stays at the /accounts path (which
arrayoperator.com proxies to the NEPOOL verifier SPA); the Array Operator
owner dashboard is the SITE ROOT. Magic-link sign-in tokens land on a page
that exchanges them: NEPOOL → /accounts, Array Operator → /login.
"""
import importlib

import pytest


def _fresh_branding(monkeypatch, **env):
    """Reload api.branding with a controlled environment."""
    for k in ("APP_URL", "AO_APP_URL", "PUBLIC_DASHBOARD_URL",
              "MAIL_FROM", "MAIL_FROM_NEPOOL", "MAIL_FROM_AO"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import api.branding as b
    return importlib.reload(b)


def test_default_nepool(monkeypatch):
    b = _fresh_branding(monkeypatch)
    assert b.brand_name("nepool") == "NEPOOL Operator"
    assert b.brand_name(None) == "NEPOOL Operator"
    # Post-fold: the nepool product's home is the folded domain; its dashboard
    # is the /accounts SPA proxy there. The dying domain must not be emitted.
    assert b.app_url("nepool") == "https://arrayoperator.com"
    assert b.dashboard_url("nepool") == "https://arrayoperator.com/accounts"


def test_array_operator_brand_name(monkeypatch):
    b = _fresh_branding(monkeypatch)
    assert b.brand_name("array_operator") == "Array Operator"


def test_array_operator_defaults_to_its_own_live_domain(monkeypatch):
    """AO now defaults to arrayoperator.com (no env flip), and its owner
    dashboard is the ROOT — NOT /accounts (which is the NEPOOL SPA proxy)."""
    b = _fresh_branding(monkeypatch)
    assert b.app_url("array_operator") == "https://arrayoperator.com"
    assert b.dashboard_url("array_operator") == "https://arrayoperator.com"


def test_array_operator_env_override(monkeypatch):
    """AO_APP_URL still overrides (e.g. staging/preview). Dashboard stays root."""
    b = _fresh_branding(monkeypatch, AO_APP_URL="https://staging.arrayoperator.com")
    assert b.app_url("array_operator") == "https://staging.arrayoperator.com"
    assert b.dashboard_url("array_operator") == "https://staging.arrayoperator.com"
    # NEPOOL unaffected (default folded home, not the AO_APP_URL override).
    assert b.dashboard_url("nepool") == "https://arrayoperator.com/accounts"


def test_magic_link_url_is_product_correct(monkeypatch):
    """The sign-in token must land on each product's OWN brand + a page that
    exchanges it: NEPOOL /accounts, Array Operator /login."""
    b = _fresh_branding(monkeypatch)
    assert b.magic_link_url("nepool", "TOK123") == \
        "https://arrayoperator.com/accounts/?token=TOK123"
    assert b.magic_link_url("array_operator", "TOK123") == \
        "https://arrayoperator.com/login?token=TOK123"
    # Unknown/blank product is treated as NEPOOL.
    assert b.magic_link_url(None, "T").startswith("https://arrayoperator.com/accounts/?token=")


def test_magic_link_honors_overrides(monkeypatch):
    b = _fresh_branding(
        monkeypatch,
        PUBLIC_DASHBOARD_URL="https://nepooloperator.com/accounts",
        AO_APP_URL="https://arrayoperator.com",
    )
    assert b.magic_link_url("array_operator", "X") == "https://arrayoperator.com/login?token=X"


def test_unknown_product_defaults_to_nepool(monkeypatch):
    b = _fresh_branding(monkeypatch)
    assert b.brand_name("something_else") == "NEPOOL Operator"
    assert b.dashboard_url("") == "https://arrayoperator.com/accounts"


def test_trailing_slashes_stripped(monkeypatch):
    b = _fresh_branding(monkeypatch, AO_APP_URL="https://arrayoperator.com/")
    assert b.app_url("array_operator") == "https://arrayoperator.com"
    assert b.dashboard_url("array_operator") == "https://arrayoperator.com"


# ── product-correct From / Reply-To ───────────────────────────────────────────

def test_from_address_is_product_correct(monkeypatch):
    b = _fresh_branding(monkeypatch)
    # NEPOOL still sends from its own (Resend-verified) domain — email domain
    # verification outlives the website sunset; MAIL_FROM_NEPOOL overrides.
    assert b.from_address("nepool") == "NEPOOL Operator <admin@nepooloperator.com>"
    # Array Operator now sends from its OWN verified domain (arrayoperator.com
    # was verified via the Resend /domains API; see branding.from_address docstring).
    ao = b.from_address("array_operator")
    assert ao.startswith("Array Operator <")
    assert "arrayoperator.com" in ao  # AO's own verified domain


def test_from_address_env_overrides(monkeypatch):
    b = _fresh_branding(
        monkeypatch,
        MAIL_FROM_AO="Array Operator <hello@arrayoperator.com>",
        MAIL_FROM_NEPOOL="NEPOOL Operator <reports@nepooloperator.com>",
    )
    assert b.from_address("array_operator") == "Array Operator <hello@arrayoperator.com>"
    assert b.from_address("nepool") == "NEPOOL Operator <reports@nepooloperator.com>"


def test_reply_to_is_monitored_inbox(monkeypatch):
    b = _fresh_branding(monkeypatch)
    assert b.reply_to_address("array_operator") == "admin@solaroperator.org"
    assert b.reply_to_address("nepool") == "admin@solaroperator.org"
