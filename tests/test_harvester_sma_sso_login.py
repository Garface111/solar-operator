"""SMA login is cross-origin SSO aware (the "login outcome=no-form" failures).

`vendors/sma.login_url` returns the ennexOS PORTAL, but SMA authenticates on
Keycloak at a SEPARATE origin (login.sma.energy). The generic login routine
polled ~6s for a form on the portal origin, found none mid-redirect, and returned
"no-form" → the engine recorded `login_failed`. Live prod 2026-07-19: 79 such
failures in 24h against one real SMA account, every one carrying that string.

No live portal is touched here — these drive the login routine against a fake
page, because the whole point of the fix is to stop burning real login attempts.
"""
from __future__ import annotations

import asyncio

from api.harvester import login as login_mod
from api.harvester.login import SSO_AUTH_HOSTS, HINTS, hint_key_for, on_auth_origin


class _El:
    def __init__(self, page, kind):
        self._page, self.kind = page, kind

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def fill(self, value, timeout=None):
        self._page.filled[self.kind] = value

    async def click(self, timeout=None):
        self._page.clicked = True

    async def text_content(self):
        return "Sign in"

    async def get_attribute(self, name):
        return None

    async def press(self, key):
        self._page.clicked = True

    async def evaluate(self, expr):
        return "button"


class _FakePage:
    """Minimal Playwright-page stand-in.

    `redirect_after` = how many wait_for_url polls before the portal bounces us
    to the identity provider; None means it never bounces (the silent-SSO case).
    """

    def __init__(self, start_url, auth_url=None, has_form_on_auth=True):
        self.url = start_url
        self._auth_url = auth_url
        self._has_form = has_form_on_auth
        self.filled: dict[str, str] = {}
        self.clicked = False

    async def wait_for_url(self, predicate, timeout=None):
        if self._auth_url is None:
            raise TimeoutError("no redirect")
        self.url = self._auth_url
        if not predicate(self.url):
            raise TimeoutError("wrong origin")

    async def query_selector_all(self, selector):
        on_auth = self._auth_url is not None and self.url == self._auth_url
        if not (on_auth and self._has_form):
            return []
        if "password" in selector.lower() or selector == 'input[type="password"]':
            return [_El(self, "pass")]
        if "button" in selector.lower() or "submit" in selector.lower():
            return [_El(self, "btn")]
        return [_El(self, "user")]

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def goto(self, url, **kw):
        self.url = url

    async def evaluate(self, expr, arg=None):
        return None


def _login(page, provider="sma"):
    return asyncio.run(
        login_mod.perform_login(page, "owner@example.com", "pw", provider))


# ── configuration ───────────────────────────────────────────────────────────

def test_sma_declares_its_keycloak_auth_origin():
    assert SSO_AUTH_HOSTS["sma"] == ("login.sma.energy",)
    # The existing SMA form hints are the Keycloak ones — keep that path wired.
    assert hint_key_for("sma") == "sma"
    assert HINTS["sma"]["btn"].startswith("#kc-login")


def test_on_auth_origin_matches_only_the_identity_provider():
    hosts = SSO_AUTH_HOSTS["sma"]
    assert on_auth_origin("https://login.sma.energy/realms/x/protocol/openid", hosts)
    assert not on_auth_origin("https://ennexos.sunnyportal.com/dashboard", hosts)


def test_only_sma_opts_into_the_sso_wait():
    """Chint/GMP/SmartHub are healthy on the tight generic path — don't loosen
    the poller for every vendor to fix one."""
    for p in ("chint", "gmp", "vec", "fronius", "cmp", "eversource"):
        assert SSO_AUTH_HOSTS.get(hint_key_for(p)) is None


# ── behavior ────────────────────────────────────────────────────────────────

def test_sma_waits_for_the_cross_origin_redirect_then_fills_keycloak():
    page = _FakePage("https://ennexos.sunnyportal.com/",
                     auth_url="https://login.sma.energy/realms/sma/auth")
    assert _login(page) == "submitted"
    assert page.url.startswith("https://login.sma.energy")
    assert page.filled.get("pass") == "pw"


def test_no_redirect_reads_as_a_silent_sso_resume_not_a_login_failure():
    """When the IdP session is still live, ennexOS re-authenticates silently and
    no form ever renders. Calling that `no-form` charged a login failure to a
    session that was actually fine — the bug. It is now reported distinctly so
    the engine verifies auth state instead."""
    page = _FakePage("https://ennexos.sunnyportal.com/", auth_url=None)
    assert _login(page) == "sso-resumed"
    assert page.filled == {}, "a silent resume must not type the password"


def test_keycloak_reached_but_serving_no_form_is_also_a_resume():
    """Keycloak answering prompt=none with a redirect instead of a form is the
    same silent-resume shape — it must not be charged as a login failure either.
    The engine still records login_failed if we turn out to be logged out, so no
    real signal is suppressed."""
    page = _FakePage("https://ennexos.sunnyportal.com/",
                     auth_url="https://login.sma.energy/realms/sma/auth",
                     has_form_on_auth=False)
    assert _login(page) == "sso-resumed"
    assert page.filled == {}


def test_non_sso_provider_keeps_the_generic_no_form_contract():
    page = _FakePage("https://portal.example.com/", auth_url=None)
    assert _login(page, provider="gmp") == "no-form"


def test_sso_form_poll_budget_is_larger_than_the_generic_one():
    assert login_mod._SSO_FORM_POLL_TRIES > login_mod._FORM_POLL_TRIES
