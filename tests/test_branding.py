"""Tests for product-aware branding (api/branding.py) and its use in the
magic-link + onboarding email URLs.

Safe-by-default contract: with no AO_APP_URL set, array_operator falls back to
the working NEPOOL domain (never a dead arrayoperator.com link).
"""
import importlib

import pytest


def _fresh_branding(monkeypatch, **env):
    """Reload api.branding with a controlled environment."""
    for k in ("APP_URL", "AO_APP_URL", "PUBLIC_DASHBOARD_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import api.branding as b
    return importlib.reload(b)


def test_default_nepool(monkeypatch):
    b = _fresh_branding(monkeypatch)
    assert b.brand_name("nepool") == "NEPOOL Operator"
    assert b.brand_name(None) == "NEPOOL Operator"
    assert b.app_url("nepool") == "https://nepooloperator.com"
    assert b.dashboard_url("nepool") == "https://nepooloperator.com/accounts"


def test_array_operator_brand_name(monkeypatch):
    b = _fresh_branding(monkeypatch)
    assert b.brand_name("array_operator") == "Array Operator"


def test_array_operator_falls_back_to_working_domain_by_default(monkeypatch):
    """CRITICAL: with no AO_APP_URL, AO must NOT emit a dead arrayoperator.com
    link — it falls back to the NEPOOL domain which proxies /accounts today."""
    b = _fresh_branding(monkeypatch)
    assert b.app_url("array_operator") == "https://nepooloperator.com"
    assert b.dashboard_url("array_operator") == "https://nepooloperator.com/accounts"


def test_array_operator_flips_with_env(monkeypatch):
    """One env var flips AO to its own domain once it's live."""
    b = _fresh_branding(monkeypatch, AO_APP_URL="https://arrayoperator.com")
    assert b.app_url("array_operator") == "https://arrayoperator.com"
    assert b.dashboard_url("array_operator") == "https://arrayoperator.com/accounts"
    # NEPOOL unaffected.
    assert b.dashboard_url("nepool") == "https://nepooloperator.com/accounts"


def test_unknown_product_defaults_to_nepool(monkeypatch):
    b = _fresh_branding(monkeypatch)
    assert b.brand_name("something_else") == "NEPOOL Operator"
    assert b.dashboard_url("") == "https://nepooloperator.com/accounts"


def test_trailing_slashes_stripped(monkeypatch):
    b = _fresh_branding(monkeypatch, AO_APP_URL="https://arrayoperator.com/")
    assert b.app_url("array_operator") == "https://arrayoperator.com"
    assert b.dashboard_url("array_operator") == "https://arrayoperator.com/accounts"
