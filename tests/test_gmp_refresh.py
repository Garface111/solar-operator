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

def _make_tenant(db, suffix: str | None = None, *, capture_mode: str | None = "device") -> Tenant:
    """Defaults to device (extension) mode: that is the one mode the reauth
    email's "the extension will capture a fresh session" instruction is true
    for, and these tests are about the threshold/dedup mechanics, not about
    which tenants should receive the email at all — that gate has its own
    tests below."""
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
        capture_mode=capture_mode,
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
    # Pre-existing gap fixed in passing: the scheduler has passed product= for a
    # while (branding is per-tenant), but this assertion never picked it up and
    # was failing before the capture_mode work in this file too.
    mock_notify.assert_called_once_with(
        to=tenant_email, name="Refresh Test Solar", product="nepool"
    )
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


def test_scheduler_does_not_renotify_after_threshold_crossed():
    """SPAM GUARD (the Bruce-hourly-email fix): a GMP session that has ALREADY
    crossed the failure threshold and keeps failing must NOT re-send the reauth
    email every hourly run. The owner is notified once on the crossing; later
    failures stay silent. Before the fix, '>= 3' re-sent on every run, so a
    genuinely-revoked session emailed the owner hourly (prod sessions reached
    20-70+ failures = that many duplicate emails)."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="renotify")
        db.flush()
        sess = _make_session(
            db, t.id,
            expires_at=datetime.utcnow() + timedelta(days=1),
            failures=10,  # already long past the threshold
        )
        db.commit()
        sess_id = sess.id
        tenant_email = t.contact_email

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"

    with patch("api.gmp_refresh.httpx.post", return_value=resp), \
         patch("api.scheduler.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.scheduler.send_internal_alert"):
        result = refresh_expiring_gmp_tokens()

    # Our already-failed session keeps failing but must NOT re-notify its owner.
    # (Filter by OUR email so other sessions in the shared test DB can't mask it.)
    assert sess_id in result["failed"]
    my_emails = [c for c in mock_notify.call_args_list
                 if c.kwargs.get("to") == tenant_email]
    assert my_emails == [], f"reauth email was re-sent to {tenant_email}"

    with SessionLocal() as db:
        updated = db.get(UtilitySession, sess_id)
        assert updated.refresh_failures == 11  # still climbs for diagnostics


def test_scheduler_notifies_exactly_once_on_threshold_crossing():
    """Complement: the owner IS notified on the run that crosses the threshold
    (failures 2 -> 3), so the fix silences the spam without silencing the genuine
    one-time nudge."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="crossonce")
        db.flush()
        _make_session(db, t.id,
                      expires_at=datetime.utcnow() + timedelta(days=1), failures=2)
        db.commit()
        tenant_email = t.contact_email

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    with patch("api.gmp_refresh.httpx.post", return_value=resp), \
         patch("api.scheduler.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.scheduler.send_internal_alert"):
        refresh_expiring_gmp_tokens()

    my_emails = [c for c in mock_notify.call_args_list
                 if c.kwargs.get("to") == tenant_email]
    assert len(my_emails) == 1  # notified exactly once, on the crossing


def test_scheduler_dedup_emails_per_tenant_across_dup_sessions():
    """TENANT-LEVEL de-dup (the dup-session amplification fix): when a tenant has
    several DUPLICATE gmp session rows for the same operator login (prod
    2026-06-21: up to 6, all customer_number NULL), one outage must email the
    operator AT MOST ONCE — only for the authoritative (newest-captured) session
    — not once per dup row. The older zombie rows still climb their failure
    counts for diagnostics but stay silent."""
    from api.scheduler import refresh_expiring_gmp_tokens, _GMP_REAUTH_NOTIFY_AFTER

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="dedup")
        db.flush()
        tid = t.id
        tenant_email = t.contact_email
        base = datetime.utcnow()
        sess_ids = []
        # Three dup rows for ONE login, all expiring and all one short of the
        # threshold so they ALL cross this run; distinct captured_at so the
        # newest (i=2) is unambiguously authoritative.
        for i in range(3):
            s = UtilitySession(
                tenant_id=tid, provider="gmp",
                api_token="old_jwt_token",
                refresh_token="rt_dup_%d_" % i + "x" * 24,
                expires_at=base + timedelta(days=1),
                captured_at=base - timedelta(hours=3 - i),
                refresh_failures=_GMP_REAUTH_NOTIFY_AFTER - 1,
            )
            db.add(s)
            db.flush()
            sess_ids.append(s.id)
        db.commit()

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"

    with patch("api.gmp_refresh.httpx.post", return_value=resp), \
         patch("api.scheduler.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.scheduler.send_internal_alert"):
        result = refresh_expiring_gmp_tokens()

    # All three dup rows failed (and climbed), but only ONE reauth email fired
    # for this tenant — for the newest-captured (authoritative) session.
    assert all(sid in result["failed"] for sid in sess_ids)
    my_emails = [c for c in mock_notify.call_args_list
                 if c.kwargs.get("to") == tenant_email]
    assert len(my_emails) == 1, (
        f"expected 1 tenant-level email, got {len(my_emails)} "
        "(dup sessions multiplied the reauth nudge)"
    )


# ─── capture-mode gate: the extension-instruction email must only reach ────
# ─── tenants who actually use the extension ─────────────────────────────────

def test_cloud_tenant_does_not_get_the_extension_email():
    """A cloud-capture tenant's session dies the same way a device tenant's
    does, but 'log in once — the extension will capture a fresh session' is
    not how Cloud Capture recovers (it logs in server-side from a stored
    password). Sending it anyway tells the customer to do something that
    cannot fix their account. The internal alert must still fire, so ops is
    never silently unaware of a dead session."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="cloud3", capture_mode="cloud")
        db.flush()
        sess = _make_session(
            db, t.id, expires_at=datetime.utcnow() + timedelta(days=1), failures=2,
        )
        db.commit()
        sess_id = sess.id

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    with patch("api.gmp_refresh.httpx.post", return_value=resp), \
         patch("api.scheduler.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.scheduler.send_internal_alert") as mock_alert:
        result = refresh_expiring_gmp_tokens()

    assert sess_id in result["failed"]
    mock_notify.assert_not_called()
    mock_alert.assert_called_once()  # ops still hears about it


def test_unset_capture_mode_also_withholds_the_email():
    """Tenants predating the capture_mode field (or never fully onboarded)
    read as capture_mode=None — treated the same as cloud, not as device.
    A missing field must never default to the customer-facing send."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="unset3", capture_mode=None)
        db.flush()
        sess = _make_session(
            db, t.id, expires_at=datetime.utcnow() + timedelta(days=1), failures=2,
        )
        db.commit()
        sess_id = sess.id

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    with patch("api.gmp_refresh.httpx.post", return_value=resp), \
         patch("api.scheduler.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.scheduler.send_internal_alert") as mock_alert:
        result = refresh_expiring_gmp_tokens()

    assert sess_id in result["failed"]
    mock_notify.assert_not_called()
    mock_alert.assert_called_once()


def test_device_tenant_still_gets_the_email():
    """The gate must not become a blanket suppression — the one mode the
    instruction is true for must be unaffected."""
    from api.scheduler import refresh_expiring_gmp_tokens

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="device3", capture_mode="device")
        db.flush()
        sess = _make_session(
            db, t.id, expires_at=datetime.utcnow() + timedelta(days=1), failures=2,
        )
        db.commit()
        sess_id = sess.id
        tenant_email = t.contact_email

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    with patch("api.gmp_refresh.httpx.post", return_value=resp), \
         patch("api.scheduler.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.scheduler.send_internal_alert"):
        result = refresh_expiring_gmp_tokens()

    assert sess_id in result["failed"]
    my_emails = [c for c in mock_notify.call_args_list
                 if c.kwargs.get("to") == tenant_email]
    assert len(my_emails) == 1


def test_final_warning_also_respects_the_capture_mode_gate():
    """gmp_final_expiry_warnings is the second, UNCONDITIONAL send site (fires
    on bare expiry, independent of the failure counter) — it carries the same
    wrong instruction and needs the same gate.

    It re-imports from .notify LOCALLY inside its own body, so the patch
    target is api.notify (matching test_coop_session_realert.py's pattern),
    not api.scheduler — patching the latter here would silently patch nothing
    and let the real functions run."""
    from api.scheduler import gmp_final_expiry_warnings, _GMP_FINAL_WARN_DAYS

    with SessionLocal() as db:
        t = _make_tenant(db, suffix="finalcloud", capture_mode="cloud")
        db.flush()
        _make_session(
            db, t.id,
            expires_at=datetime.utcnow() + timedelta(days=_GMP_FINAL_WARN_DAYS - 1),
        )
        db.commit()

    with patch("api.notify.send_gmp_reauth_needed_email") as mock_notify, \
         patch("api.notify.send_internal_alert") as mock_alert:
        gmp_final_expiry_warnings()

    mock_notify.assert_not_called()
    mock_alert.assert_called_once()
