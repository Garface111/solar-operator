"""Cloud Capture lockout guard — the counter, the self-healing pause, the alert.

Regression cover for two real prod bugs (2026-07-19, Bruce's SMA login):

1. The guard NEVER fired. `record_health` cleared `harvest_fails` on ANY ok, so a
   credential alternating login_failed → ok reset the counter every other run and
   never reached MAX_LOGIN_FAILS. Live: 80 ok / 79 login_failed in 24h against one
   real SMA account, `harvest_fails` parked at 1.
2. When it DID fire, the pause was permanent and silent — `_is_due` returned False
   forever and nothing told anyone. That is the self-disarm Ford banned.

These tests drive the counter directly rather than a browser: the lockout logic is
pure and must never need a live portal login to verify (hammering a real account
is the exact thing the guard exists to prevent).
"""
from __future__ import annotations

import base64
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api import crypto
from api.db import SessionLocal
from api.harvester import credentials as cc
from api.harvester.scheduler import (
    FAIL_BACKOFF,
    MAX_LOGIN_FAILS,
    PAUSED_RETRY,
    _is_due,
    account_key,
    accounts_with_recent_fresh_login,
    coordinate_account_fails,
)
from api.models import HarvestRun, InverterAlertState, PortalCredential, Tenant, now

TENANT = "ten_lockout_t1"
TENANT2 = "ten_lockout_t2"


@pytest.fixture(autouse=True)
def _armed_crypto():
    """Cloud Capture refuses to store a password unless encryption-at-rest is
    armed, so the vault path needs a key to exercise at all."""
    old = os.environ.get(crypto.ENV_KEY)
    os.environ[crypto.ENV_KEY] = base64.urlsafe_b64encode(b"k" * 32).decode()
    crypto._cache.clear()
    yield
    if old is None:
        os.environ.pop(crypto.ENV_KEY, None)
    else:
        os.environ[crypto.ENV_KEY] = old
    crypto._cache.clear()


def _tenant(db, tid: str) -> Tenant:
    t = db.get(Tenant, tid)
    if t is None:
        t = Tenant(id=tid, name=tid, contact_email=f"{tid}@example.com",
                   tenant_key=f"key_{tid}", active=True, is_demo=True)
        db.add(t)
        db.flush()
    return t


def _cred(db, tid: str = TENANT, provider: str = "sma",
          username: str = "owner@example.com") -> PortalCredential:
    _tenant(db, tid)
    row = cc.upsert_credential(db, tid, provider, username, "pw", enable=True)
    db.flush()
    row.harvest_fails = 0
    row.last_harvest_ok = None
    row.last_harvest_at = None
    db.flush()
    return row


def _run(db, cred, *, ok: bool, status: str, fresh: bool) -> None:
    cc.record_health(db, cred, ok=ok, status=status, started_at=now(),
                     fresh_login=fresh, rows_written=1 if ok else 0)
    db.flush()


# ── 1. the counter ──────────────────────────────────────────────────────────

def test_alternating_ok_and_login_failed_no_longer_resets_the_counter():
    """The EXACT live pattern: login_failed → ok → login_failed → ok …

    Every ok here is a WARM-SESSION success (fresh=False) that performed no login
    at all. It must not vouch for a password it never used, so the counter has to
    climb to the pause instead of oscillating forever.
    """
    with SessionLocal() as db:
        c = _cred(db, provider="sma", username="alt@example.com")
        for _ in range(6):
            _run(db, c, ok=False, status="login_failed", fresh=True)
            _run(db, c, ok=True, status="ok", fresh=False)
        assert c.harvest_fails >= MAX_LOGIN_FAILS, (
            "alternating warm-ok/login-failed must reach the pause, not sit at 1"
        )


def test_warm_session_ok_does_not_clear_a_standing_login_problem():
    with SessionLocal() as db:
        c = _cred(db, username="warm@example.com")
        for _ in range(MAX_LOGIN_FAILS):
            _run(db, c, ok=False, status="login_failed", fresh=True)
        assert c.harvest_fails == MAX_LOGIN_FAILS
        _run(db, c, ok=True, status="ok", fresh=False)          # warm session
        assert c.harvest_fails == MAX_LOGIN_FAILS


def test_fresh_login_ok_clears_the_counter():
    with SessionLocal() as db:
        c = _cred(db, username="fresh@example.com")
        for _ in range(MAX_LOGIN_FAILS):
            _run(db, c, ok=False, status="login_failed", fresh=True)
        assert c.harvest_fails == MAX_LOGIN_FAILS
        _run(db, c, ok=True, status="ok", fresh=True)           # real login worked
        assert c.harvest_fails == 0


def test_scrape_failures_never_count_toward_the_lockout():
    """A scrape failure happens AFTER authentication — no lockout risk, and it
    must not throttle the data path onto the 30-min login backoff."""
    with SessionLocal() as db:
        c = _cred(db, provider="chint", username="scrape@example.com")
        for _ in range(10):
            _run(db, c, ok=False, status="scrape_failed", fresh=False)
        assert c.harvest_fails == 0


# ── 2. the pause: self-healing, never permanent ─────────────────────────────

def _fake(fails=0, ok=None, age_min=None, provider="sma"):
    last = None if age_min is None else now() - timedelta(minutes=age_min)
    return SimpleNamespace(harvest_fails=fails, last_harvest_ok=ok,
                           last_harvest_at=last, provider=provider,
                           username_lc="x@example.com")


def test_paused_login_is_not_retried_on_the_tight_loop():
    assert not _is_due(_fake(fails=MAX_LOGIN_FAILS, ok=False, age_min=30), now())


def test_paused_login_self_heals_on_the_slow_retry():
    """Retry-forever-with-backoff, never give-up-forever."""
    age = int(PAUSED_RETRY.total_seconds() / 60) + 1
    assert _is_due(_fake(fails=MAX_LOGIN_FAILS, ok=False, age_min=age), now())
    # …and stays due however long it has been paused (no permanent give-up).
    assert _is_due(_fake(fails=99, ok=False, age_min=60 * 24 * 30), now())


def test_failed_login_backs_off_then_retries():
    assert not _is_due(_fake(fails=1, ok=False, age_min=1), now())
    assert _is_due(_fake(fails=1, ok=False,
                         age_min=int(FAIL_BACKOFF.total_seconds() / 60) + 1), now())


def test_recovered_warm_session_keeps_its_normal_cadence():
    """A standing fail counter must not throttle a credential whose warm session
    is serving data fine — the counter stays honest, the data path stays fast."""
    assert _is_due(_fake(fails=1, ok=True, age_min=10, provider="sma"), now())


def test_rearm_clears_the_pause_immediately():
    with SessionLocal() as db:
        c = _cred(db, username="rearm@example.com")
        for _ in range(MAX_LOGIN_FAILS + 2):
            _run(db, c, ok=False, status="login_failed", fresh=True)
        db.commit()
        cc.rearm(db, TENANT, "sma", "rearm@example.com")
        db.commit()
        c2 = db.get(PortalCredential, c.id)
        assert c2.harvest_fails == 0 and c2.last_harvest_at is None
        assert _is_due(c2, now())


# ── 3. one portal account, many tenants ─────────────────────────────────────

def test_fails_are_coordinated_per_portal_account_not_per_tenant():
    """Bruce's one SMA login exists under 3 tenants (his Chint login under 8).

    Per-row counters made the guard per-tenant while the lockout is per-account:
    N tenants each burned their own MAX_LOGIN_FAILS attempts against a single
    provider account. A bad account must stop everyone sharing it.
    """
    rows = [_fake(fails=MAX_LOGIN_FAILS, ok=False, age_min=30),
            _fake(fails=0, ok=False, age_min=30)]
    fails = coordinate_account_fails(rows)
    key = account_key("sma", "x@example.com")
    assert fails[key] == MAX_LOGIN_FAILS
    assert not _is_due(rows[1], now(), fails[key]), (
        "a sibling tenant must not keep hammering an account already at the pause"
    )


def test_a_sibling_that_logged_in_fine_vouches_for_the_account():
    """One tenant's local trouble must not pause a login that demonstrably works
    — otherwise a single flaky tenant takes the whole account dark."""
    rows = [_fake(fails=MAX_LOGIN_FAILS, ok=False, age_min=30),
            _fake(fails=0, ok=True, age_min=1)]
    fails = coordinate_account_fails(rows)
    assert fails[account_key("sma", "x@example.com")] == 0


def test_a_recent_successful_fresh_login_vouches_even_while_currently_failing():
    """An intermittently-succeeding login (SMA logs in ~1 try in 2) reads as
    'currently failing' between successes. That is honest but is NOT a lockout —
    the portal accepted our password minutes ago — so it must not pause the
    account and take a half-healthy capture dark."""
    rows = [_fake(fails=MAX_LOGIN_FAILS + 1, ok=False, age_min=5),
            _fake(fails=1, ok=False, age_min=5)]
    key = account_key("sma", "x@example.com")
    assert coordinate_account_fails(rows)[key] >= MAX_LOGIN_FAILS
    assert coordinate_account_fails(rows, vouched={key})[key] == 0


def test_recent_fresh_login_lookup_reads_the_run_log_not_the_last_run():
    with SessionLocal() as db:
        c = _cred(db, provider="sma", username="vouch@example.com")
        _run(db, c, ok=True, status="ok", fresh=True)       # succeeded…
        _run(db, c, ok=False, status="login_failed", fresh=True)   # …then failed
        db.commit()
        assert c.last_harvest_ok is False                   # last run says "bad"
        assert account_key("sma", "vouch@example.com") in \
            accounts_with_recent_fresh_login(db)            # history says "let us in"


def test_an_account_that_never_gets_in_is_not_vouched():
    with SessionLocal() as db:
        c = _cred(db, provider="sma", username="locked@example.com")
        for _ in range(MAX_LOGIN_FAILS + 1):
            _run(db, c, ok=False, status="login_failed", fresh=True)
        db.commit()
        assert account_key("sma", "locked@example.com") not in \
            accounts_with_recent_fresh_login(db)


# ── 4. the pause is LOUD, and it clears ────────────────────────────────────

def test_paused_login_emits_a_deduped_operator_alert_and_clears_on_recovery(monkeypatch):
    from api.harvester import lockout_alert

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(lockout_alert, "send_internal_alert",
                        lambda subject, body: sent.append((subject, body)) or True)

    with SessionLocal() as db:
        c = _cred(db, tid=TENANT2, provider="sma", username="paused@example.com")
        for _ in range(MAX_LOGIN_FAILS):
            _run(db, c, ok=False, status="login_failed", fresh=True)
        db.add(HarvestRun(tenant_id=TENANT2, provider="sma",
                          username_lc="paused@example.com", started_at=now(),
                          ended_at=now(), status="login_failed",
                          logged_in_fresh=True, rows_written=0,
                          detail="login outcome=no-form, not authenticated"))
        db.commit()

    mine = lockout_alert._incident_key("sma", "paused@example.com")

    r1 = lockout_alert.run_login_lockout_watchdog()
    assert mine in r1["alerted"], "a tripped pause must ALERT, not go quiet"
    assert len(sent) == 1
    subject, body = sent[0]
    assert "paused" in subject.lower()
    assert "paused@example.com" in body
    assert "no-form" in body, "the alert must carry the real error, not a vague nudge"

    # Deduped: still paused, but no second alert inside the re-alert window.
    r2 = lockout_alert.run_login_lockout_watchdog()
    assert mine not in r2["alerted"]
    assert len(sent) == 1

    # Recovery clears the incident so the NEXT lockout is a fresh, loud alert.
    with SessionLocal() as db:
        cred = db.execute(select(PortalCredential).where(
            PortalCredential.tenant_id == TENANT2,
            PortalCredential.username_lc == "paused@example.com")).scalar_one()
        _run(db, cred, ok=True, status="ok", fresh=True)
        db.commit()

    lockout_alert.run_login_lockout_watchdog()
    with SessionLocal() as db:
        assert db.execute(select(InverterAlertState).where(
            InverterAlertState.incident_key == mine)).scalar_one_or_none() is None


def test_alert_incident_key_is_namespaced_out_of_the_inverter_sweep():
    """inverter_alert_state is shared; the sweep reconciles only keys containing
    '|' (memory: shared-alert-state-table). Ours must never collide."""
    from api.harvester import lockout_alert
    key = lockout_alert._incident_key("sma", "Owner@Example.com")
    assert key == "cloud_capture_login_paused:sma:owner@example.com"
    assert "|" not in key
