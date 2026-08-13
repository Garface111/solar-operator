"""Fronius login is cross-origin SSO aware (the 2026-08 "no-form" failures).

Solar.web moved auth to Fronius's own IdP — WSO2 on login.fronius.com (observed
live 2026-08-13) — and put a Cookiebot consent wall on the "/" landing. The
harvester polled ~6s for a form on solarweb.com, found none, and returned
"no-form" → login_failed. Live prod: Colleen's Danville login failed 32
consecutive fresh logins with exactly that string (and Bruce's briefly on
2026-08-10), while warm sessions kept working — so the breakage only showed
when a session died.

Fix shape (same as SMA's): enter at /Account/ExternalLogin (which 302s straight
to the IdP, skipping the consent-walled landing) and register login.fronius.com
in SSO_AUTH_HOSTS so the login routine waits for the handoff with the SSO
budget. The WSO2 form fields were already in HINTS["fronius"].

No live portal is touched — these drive the login routine against a fake page.
"""
from __future__ import annotations

import asyncio

from api.harvester import login as login_mod
from api.harvester.login import HINTS, SSO_AUTH_HOSTS, hint_key_for, on_auth_origin
from api.harvester.vendors.fronius import FroniusVendor

from test_harvester_sma_sso_login import _FakePage


def _login(page, provider="fronius"):
    return asyncio.run(
        login_mod.perform_login(page, "owner@example.com", "pw", provider))


# ── configuration ───────────────────────────────────────────────────────────

def test_fronius_declares_its_idp_origin():
    assert SSO_AUTH_HOSTS["fronius"] == ("login.fronius.com",)
    # The WSO2 form hints predate the migration and match the live login.do
    # form (verified 2026-08-13): #usernameUserInput / #password / #login-button.
    assert hint_key_for("fronius") == "fronius"
    assert HINTS["fronius"]["user"] == "#usernameUserInput"
    assert "#login-button" in HINTS["fronius"]["btn"]


def test_fronius_login_url_enters_at_the_oauth_handoff():
    url = asyncio.run(FroniusVendor().login_url(None))
    assert url == "https://www.solarweb.com/Account/ExternalLogin", (
        "the '/' landing fronts a consent wall + SPA login bounce — the "
        "harvester must enter at the OAuth handoff, not hunt a form there"
    )


def test_on_auth_origin_is_the_idp_not_solarweb():
    hosts = SSO_AUTH_HOSTS["fronius"]
    assert on_auth_origin(
        "https://login.fronius.com/authenticationendpoint/login.do?x=1", hosts)
    assert not on_auth_origin("https://www.solarweb.com/Account/ExternalLogin", hosts)


# ── behavior ────────────────────────────────────────────────────────────────

def test_fronius_already_on_the_idp_fills_the_wso2_form():
    """page.goto follows the ExternalLogin 302, so a logged-out session lands
    on login.do BEFORE perform_login runs — the auth-origin wait must accept
    'already there' instantly and fill the form."""
    auth = "https://login.fronius.com/authenticationendpoint/login.do?client_id=x"
    page = _FakePage(auth, auth_url=auth)
    assert _login(page) == "submitted"
    assert page.filled.get("pass") == "pw"


def test_fronius_redirects_to_the_idp_then_fills():
    page = _FakePage(
        "https://www.solarweb.com/Account/ExternalLogin",
        auth_url="https://login.fronius.com/authenticationendpoint/login.do")
    assert _login(page) == "submitted"
    assert page.url.startswith("https://login.fronius.com")


def test_fronius_no_redirect_is_a_silent_resume_not_a_login_failure():
    """A live IdP session bounces ExternalLogin straight back to solarweb with
    no form — that used to read as 'no-form' and burn a login failure. It is a
    resume; the engine still records login_failed if we're really logged out."""
    page = _FakePage("https://www.solarweb.com/Account/ExternalLogin",
                     auth_url=None)
    assert _login(page) == "sso-resumed"
    assert page.filled == {}, "a silent resume must not type the password"
