"""Generic portal login — a faithful Playwright port of the extension's
``soFillLoginForm`` + ``findBtn`` (extension/background.js:2658).

The extension has driven these exact forms in production against the live login
DOMs (SMA Keycloak, Fronius WSO2, GMP, SmartHub/NISC's two skins), so the
selector knowledge here is grounded, not guessed. The port keeps the same
three-path shape:
  * combined username+password form (the happy path),
  * password-only second step,
  * identifier-first (WSO2/Keycloak): type username → continue → wait for the
    password step to render (AJAX or navigation) → fill it.

Returns one of: "submitted" | "filled-username" | "no-form".
"""
from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("harvester.login")

# Per-vendor grounded selectors (ported verbatim from background.js:2664). The
# generic matchers below are the safety net when a portal reworks its form.
HINTS: dict[str, dict[str, str]] = {
    "sma": {
        "user": "#username",
        "pass": "#password",
        "btn": "#kc-login, button[type=\"submit\"]",
    },
    "fronius": {
        "user": "#usernameUserInput",
        "pass": "#password",
        "btn": "#login-button, [data-testid=\"login-page-continue-login-button\"], button[type=\"submit\"]",
    },
    "gmp": {
        "user": "input[type=\"email\"], #username, input[name=\"username\" i], input[name=\"email\" i]",
        "pass": "#password, input[name=\"password\" i]",
        "btn": "button[type=\"submit\"], button[id*=\"login\" i], button[id*=\"signin\" i]",
    },
    # Eversource MyAccount (Okta-backed React form on www.eversource.com).
    # Username is typically email; Okta may also present identifier-first.
    "eversource": {
        "user": ("input[type=\"email\"], input[name=\"username\" i], "
                 "input[name=\"identifier\" i], input[id*=\"username\" i], "
                 "input[id*=\"email\" i], input[autocomplete=\"username\"]"),
        "pass": ("input[type=\"password\"], input[name=\"password\" i], "
                 "input[id*=\"password\" i], input[autocomplete=\"current-password\"]"),
        "btn": ("button[type=\"submit\"], input[type=\"submit\"], "
                "button[id*=\"login\" i], button[id*=\"signin\" i], "
                "input[value*=\"Sign\" i], input[value*=\"Log\" i]"),
    },
    # SmartHub spans two skins: legacy ASP.NET (#Login*TextBox) and the modern
    # NISC Angular SPA (Email + Password + a plain "Sign In" <button> with no
    # type=submit, inputs often NOT in a <form>). The text-scan submit finder is
    # the real safety net for the Angular skin.
    "smarthub": {
        "user": ("#LoginUsernameTextBox, input[name=\"username\" i], input[name=\"userId\" i], "
                 "input[formcontrolname*=\"email\" i], input[formcontrolname*=\"user\" i], "
                 "input[autocomplete=\"username\"], input[type=\"email\"]"),
        "pass": ("#LoginPasswordTextBox, input[name=\"password\" i], "
                 "input[formcontrolname*=\"pass\" i], input[type=\"password\"]"),
        "btn": "#LoginSubmitButton, button[type=\"submit\"], input[type=\"submit\"], button[id*=\"login\" i]",
    },
}

_INVERTER_KEYS = ("fronius", "sma", "chint")

# Generic field matchers (ported from findUser/findPass).
_GENERIC_USER = ('input[type="text"], input[type="email"], input[type="tel"], '
                 'input[name*="user" i], input[name*="email" i], '
                 'input[id*="user" i], input[id*="email" i]')
_GENERIC_PASS = 'input[type="password"]'
# Attribute-based submit matcher (ported from findBtn's byAttr).
_GENERIC_BTN = ('button[type="submit"], input[type="submit"], button[name*="login" i], '
                'button[id*="login" i], button[id*="signin" i], button[id*="next" i], '
                'button[id*="continue" i]')
_BTN_TEXT_RX = re.compile(r"\b(sign\s*in|log\s*in|log\s*on|login|continue|submit|next)\b", re.I)


def hint_key_for(provider: str) -> str:
    """A known inverter/gmp/eversource key uses its own hint; any other code is
    a SmartHub co-op (vec/wec/sh_*) and shares the one SmartHub login form."""
    p = (provider or "").lower()
    if p in ("eversource", "eversource_ma", "eversource_ct"):
        return "eversource"
    if p in HINTS:
        return p
    if p in _INVERTER_KEYS:
        return p
    return "smarthub"


async def _first_visible(page, selector: str):
    """First visible, enabled element matching `selector`, else None."""
    try:
        for el in await page.query_selector_all(selector):
            try:
                if await el.is_visible() and await el.is_enabled():
                    return el
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _find_user(page, hint):
    if hint:
        el = await _first_visible(page, hint["user"])
        if el:
            return el
    # generic, excluding password fields (they're a different query already)
    return await _first_visible(page, _GENERIC_USER)


async def _find_pass(page, hint):
    if hint:
        el = await _first_visible(page, hint["pass"])
        if el:
            return el
    return await _first_visible(page, _GENERIC_PASS)


async def _find_btn(page, hint):
    if hint and hint.get("btn"):
        el = await _first_visible(page, hint["btn"])
        if el:
            return el
    el = await _first_visible(page, _GENERIC_BTN)
    if el:
        return el
    # Text scan for a login-ish label — the Angular/React SmartHub safety net.
    try:
        for el in await page.query_selector_all('button, input[type="button"], [role="button"]'):
            try:
                if not (await el.is_visible()):
                    continue
                label = (await el.text_content()) or (await el.get_attribute("value")) or ""
                if _BTN_TEXT_RX.search(label.strip()):
                    return el
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _set_value(page, el, value: str) -> None:
    """Type into a field robustly. Playwright's fill() dispatches proper input
    events (works for React/Angular controlled inputs); fall back to a native
    setter + input/change dispatch (the exact extension trick) if fill is
    rejected by a custom widget."""
    try:
        await el.fill(value, timeout=8000)
        return
    except Exception:
        pass
    await page.evaluate(
        """([el, val]) => {
            const proto = el.tagName === 'TEXTAREA'
              ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [el, value],
    )


async def _submit(page, btn, ref) -> None:
    """Click the submit button; fall back to form.requestSubmit / Enter."""
    try:
        if btn and await btn.is_visible():
            await btn.click(timeout=8000)
            return
    except Exception:
        pass
    try:
        if ref:
            await ref.press("Enter")
    except Exception:
        pass


async def _poll_for_form(page, hint):
    """Poll ~6s for a username/password field (SSO pages lag the load event)."""
    pw = user = None
    for _ in range(20):
        pw = await _find_pass(page, hint)
        user = await _find_user(page, hint)
        if pw or user:
            break
        await asyncio.sleep(0.3)
    return pw, user


async def _click_login_entry(page) -> bool:
    """Reach the real form from a landing "Login"/"Sign in" entry. Only called
    when NO form was found, so this can't be a premature submit. Prefers
    NAVIGATING to an anchor's href (Fronius: /Account/ExternalLogin) over clicking
    — a click can be swallowed by a cookie-consent overlay (Cookiebot). Returns
    True if it navigated/clicked."""
    try:
        for el in await page.query_selector_all('a, button, [role="button"]'):
            try:
                if not await el.is_visible():
                    continue
                label = ((await el.text_content()) or "").strip()
                if not _BTN_TEXT_RX.search(label):
                    continue
                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                if tag == "a":
                    href = await el.evaluate("e => e.href || ''")   # absolute
                    if href and "#" != (await el.get_attribute("href") or "") \
                            and not href.lower().startswith("javascript"):
                        await page.goto(href, wait_until="domcontentloaded", timeout=45000)
                        return True
                await el.click(timeout=8000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def perform_login(page, username: str, password: str, provider: str) -> str:
    """Drive the portal's own login form. See module docstring for return values.
    NEVER logs the password."""
    if not username or not password:
        return "no-form"
    hint = HINTS.get(hint_key_for(provider))

    # Poll up to ~6s for a field to appear (SSO pages lag the load event).
    pw, user = await _poll_for_form(page, hint)
    if not pw and not user:
        # Many portals (Fronius/SMA) show a landing page with a "Login" button
        # that bounces to the real form on a separate SSO origin (WSO2/Keycloak).
        # No form here yet ⇒ click the login-entry link, then look again.
        if await _click_login_entry(page):
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            pw, user = await _poll_for_form(page, hint)
    if not pw and not user:
        return "no-form"

    if pw and user:                                  # combined form — happy path
        try:
            await _set_value(page, user, username)
            await _set_value(page, pw, password)
        except Exception:
            return "no-form"
        await _submit(page, await _find_btn(page, hint), pw)
        return "submitted"

    if pw and not user:                              # password-only second step
        try:
            await _set_value(page, pw, password)
        except Exception:
            return "no-form"
        await _submit(page, await _find_btn(page, hint), pw)
        return "submitted"

    # Identifier-first (WSO2/Keycloak): username → continue → wait for pw step.
    try:
        await _set_value(page, user, username)
    except Exception:
        return "no-form"
    await _submit(page, await _find_btn(page, hint), user)
    for _ in range(20):
        await asyncio.sleep(0.3)
        p2 = await _find_pass(page, hint)
        if p2:
            try:
                await _set_value(page, p2, password)
            except Exception:
                return "filled-username"
            await _submit(page, await _find_btn(page, hint), p2)
            return "submitted"
    return "filled-username"
