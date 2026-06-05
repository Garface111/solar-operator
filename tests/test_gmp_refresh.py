"""Tests for GMP token refresh module and scheduler integration."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from api.db import SessionLocal
from api.models import Tenant, UtilitySession, now
from api.gmp_refresh import GmpRefreshError, refresh_gmp_token


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_tenant(db, suffix: str | None = None) -> Tenant:
    sfx = suffix or secrets.token_hex(4)
    t = Tenant(
        id=f"ten_gmpr_{sfx}",
        name="Refresh Test Solar",
        contact_email=f"operator_{sfx}@example.com",
        tenant_key=f"sol_live_gmpr_{sfx}",
        plan="standard",
        active=True,
        created_at=now(),
        onboarding_stage="done",
    )
    db.add(t)
    return t


def _make_session(db, tenant_id: str, *, refresh_token: str | None = "rt_" + "x" * 28,
                  expires_at: datetime | None = None, failures: int = 0) -> UtilitySession:
    sess = UtilitySession(
        tenant_id=tenant_id,
        provider="gmp",
        api_token="old_jwt_token",
        refresh_token=refresh_token,
        expires_at=expires_at or datetime.utcnow() + timedelta(days=3),
        captured_at=now(),
        refresh_failures=failures,
    )
    db.add(sess)
    return sess


def _mock_200(new_jwt: str = "new_jwt_abc", expires_in: int = 1_814_400) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": new_jwt,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }
    return resp


# ─── unit tests: refresh_gmp_token ──────────────────────────────────────────

def test_refresh_success_returns_tuple():
    """Mocked 200 → (new_jwt, expires_at) tuple with correct token and future expiry."""
    new_jwt = "fresh_jwt_xyz"
    with patch("httpx.post", return_value=_mock_200(new_jwt=new_jwt)) as mock_post:
        token, expires_at = refresh_gmp_token("rt_deadbeef" + "a" * 22)

    assert token == new_jwt
    assert isinstance(expires_at, datetime)
    # 21-day window: must be ~21 days in the future (within 60s tolerance)
    expected = datetime.utcnow() + timedelta(seconds=1_814_400)
    assert abs((expires_at - expected).total_seconds()) < 60

    # Verify correct URL, headers, and body were sent
    call_kwargs = mock_post.call_args
    assert "remember_me=true" in call_kwargs.args[0]
    assert call_kwargs.kwargs["headers"]["GMP-Source"] == "web"
    assert call_kwargs.kwargs["data"]["grant_type"] == "refresh_token"
    assert call_kwargs.kwargs["data"]["client_id"] == "C978562571FC475294191C7B94DD883E"


def test_refresh_raises_on_401():
    """HTTP 401 (expired refresh token) → GmpRefreshError."""
    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    with patch("httpx.post", return_value=resp):
        with pytest.raises(GmpRefreshError, match="refresh failed: HTTP 401"):
            refresh_gmp_token("rt_expired" + "x" * 22)


def test_refresh_raises_on_network_error():
    """Network-level failure → GmpRefreshError wrapping the original exception."""
    import httpx
    with patch("httpx.post", side_effect=httpx.ConnectError("connection refused")):
        with pytest.raises(GmpRefreshError, match="network error"):
            refresh_gmp_token("rt_netfail" + "x" * 22)


# ─── integration tests: scheduler ───────────────────────────────────────────

def test_scheduler_picks_up_expiring_sessions():
    """Sessions with provider='gmp', refresh_token set, and expires_at within 7 days
    are picked up by refresh_expiring_gmp_tokens."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="pick")
        db.flush()
        sess = _make_session(db, t.id, expires_at=datetime.utcnow() + timedelta(days=2))
        db.commit()
        sess_id = sess.id

    new_jwt = "picked_up_jwt"
    mock_resp = _mock_200(new_jwt=new_jwt)
    with patch("api.gmp_refresh.httpx.post", return_value=mock_resp):
        result = refresh_expiring_gmp_tokens()

    assert sess_id in result["refreshed"]


def test_scheduler_updates_fields_on_success():
    """On a successful refresh the session gets new api_token, expires_at,
    last_refresh_at updated and refresh_failures reset to 0."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="upd")
        db.flush()
        sess = _make_session(
            db, t.id,
            expires_at=datetime.utcnow() + timedelta(days=1),
            failures=1,
        )
        db.commit()
        sess_id = sess.id

    new_jwt = "updated_jwt_value"
    with patch("api.gmp_refresh.httpx.post", return_value=_mock_200(new_jwt=new_jwt)):
        refresh_expiring_gmp_tokens()

    with SessionLocal() as db:
        updated = db.get(UtilitySession, sess_id)
        assert updated.api_token == new_jwt
        assert updated.expires_at is not None
        assert updated.expires_at > datetime.utcnow() + timedelta(days=20)
        assert updated.last_refresh_at is not None
        assert updated.refresh_failures == 0


def test_scheduler_notifies_after_3_failures():
    """After 3 consecutive failures the operator email is sent and an
    internal alert fires."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="fail3")
        db.flush()
        sess = _make_session(
            db, t.id,
            expires_at=datetime.utcnow() + timedelta(days=1),
            failures=2,  # this run will push it to 3
        )
        db.commit()
        sess_id = sess.id
        tenant_email = t.contact_email

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"

    with patch("api.gmp_refresh.httpx.post", return_value=resp), \
         patch("api.scheduler.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.scheduler.send_internal_alert") as mock_alert:
        result = refresh_expiring_gmp_tokens()

    assert sess_id in result["failed"]
    mock_notify.assert_called_once_with(to=tenant_email, name="Refresh Test Solar")
    mock_alert.assert_called_once()

    with SessionLocal() as db:
        updated = db.get(UtilitySession, sess_id)
        assert updated.refresh_failures == 3


def test_scheduler_skips_null_refresh_token():
    """Sessions where refresh_token IS NULL are not included in refresh attempts."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="null_rt")
        db.flush()
        sess = _make_session(
            db, t.id,
            refresh_token=None,
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        db.commit()
        sess_id = sess.id

    # Other tests' sessions may also be refreshed in the shared DB — give post a
    # valid 200 response so those don't error, then only check our null-RT session.
    with patch("api.gmp_refresh.httpx.post", return_value=_mock_200()):
        result = refresh_expiring_gmp_tokens()

    # Our null-RT session must not appear in either refreshed or failed
    assert sess_id not in result["refreshed"]
    assert sess_id not in result["failed"]
