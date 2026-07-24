"""Locus login diagnostics — the message must name the ACTUAL problem.

Bruce (2026-07-23) typed the company display name "Johnson Hardware and Rental"
instead of the SolarNOC username `johnson_hardware_and_rental` and was told his
correct password was rejected. These cover the slug retry + per-error messages.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from api.adapters import locus


def _resp(status: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/x-amz-json-1.1"},
        request=httpx.Request("POST", locus.COGNITO_URL),
    )


def _ok_tokens() -> httpx.Response:
    return _resp(200, {"AuthenticationResult": {
        "IdToken": "h.e.s", "RefreshToken": "r", "ExpiresIn": 3600,
    }})


def setup_function() -> None:
    locus._TOKEN_CACHE.clear()


def test_normalize_username_slugifies_display_name():
    assert locus.normalize_username("Johnson Hardware and Rental") == \
        "johnson_hardware_and_rental"
    assert locus.normalize_username("  Acme-Solar LLC ") == "acme_solar_llc"
    # Already a slug — unchanged.
    assert locus.normalize_username("johnson_hardware_and_rental") == \
        "johnson_hardware_and_rental"


def test_display_name_retries_as_slug_and_succeeds():
    """The exact Bruce case: typed display name connects via the slug retry."""
    seen: list[str] = []

    def fake_post(url, **kw):
        user = json.loads(kw["content"])["AuthParameters"]["USERNAME"]
        seen.append(user)
        if user == "johnson_hardware_and_rental":
            return _ok_tokens()
        return _resp(400, {"__type": "UserNotFoundException", "message": "User does not exist."})

    with patch("httpx.post", side_effect=fake_post):
        token = locus.get_token("Johnson Hardware and Rental", "pw")

    assert token == "h.e.s"
    assert seen == ["Johnson Hardware and Rental", "johnson_hardware_and_rental"]


def test_genuinely_missing_slug_user_says_user_not_found():
    """A username that's ALREADY a slug isn't retried — and the message names
    the real problem rather than blaming the password."""
    def fake_post(url, **kw):
        return _resp(400, {"__type": "UserNotFoundException", "message": "User does not exist."})

    with patch("httpx.post", side_effect=fake_post):
        with pytest.raises(locus.LocusUserNotFoundError) as ei:
            locus.get_token("no_such_user", "pw")

    msg = str(ei.value)
    assert "No SolarNOC user" in msg
    assert "display name" in msg


def test_lockout_is_not_reported_as_a_wrong_password():
    def fake_post(url, **kw):
        return _resp(400, {
            "__type": "NotAuthorizedException",
            "message": "Password attempts exceeded",
        })

    with patch("httpx.post", side_effect=fake_post):
        with pytest.raises(locus.LocusAuthError) as ei:
            locus.get_token("some_user", "pw")

    msg = str(ei.value)
    assert "locked" in msg.lower()
    assert "rejected the username/password" not in msg


def test_password_reset_required_says_so():
    def fake_post(url, **kw):
        return _resp(400, {"__type": "PasswordResetRequiredException", "message": "reset"})

    with patch("httpx.post", side_effect=fake_post):
        with pytest.raises(locus.LocusAuthError) as ei:
            locus.get_token("some_user", "pw")

    assert "password reset" in str(ei.value).lower()


def test_wrong_password_still_reads_as_a_credential_problem():
    def fake_post(url, **kw):
        return _resp(400, {
            "__type": "NotAuthorizedException", "message": "Incorrect username or password.",
        })

    with patch("httpx.post", side_effect=fake_post):
        with pytest.raises(locus.LocusAuthError) as ei:
            locus.get_token("some_user", "wrong")

    assert "rejected the username/password" in str(ei.value)


def test_mfa_challenge_explains_the_extra_step():
    def fake_post(url, **kw):
        return _resp(200, {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "s"})

    with patch("httpx.post", side_effect=fake_post):
        with pytest.raises(locus.LocusAuthError) as ei:
            locus.get_token("some_user", "pw")

    msg = str(ei.value)
    assert "SOFTWARE_TOKEN_MFA" in msg
    assert "no IdToken" not in msg
