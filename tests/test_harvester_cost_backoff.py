"""Cloud Capture cost discipline: failing captures back off, the browser sleeps.

Prod 2026-10-01: in 24h the harvester ran 651 Chint "no sites" + 313 SMA
"sso-resumed" failures (vs 69 successes) on the 3-minute inverter loop, with one
Chromium + Playwright driver resident around the clock — the single biggest line
on the Railway bill. These pin the two fixes without a browser or a live portal.
"""
from __future__ import annotations

import asyncio
import base64
import os
from datetime import timedelta

import pytest

from api import crypto
from api.db import SessionLocal
from api.harvester import credentials as cc
from api.harvester import scheduler as sch
from api.models import PortalCredential, Tenant, now

TENANT = "ten_costbackoff_t1"


@pytest.fixture(autouse=True)
def _armed_crypto():
    old = os.environ.get(crypto.ENV_KEY)
    os.environ[crypto.ENV_KEY] = base64.urlsafe_b64encode(b"k" * 32).decode()
    crypto._cache.clear()
    yield
    if old is None:
        os.environ.pop(crypto.ENV_KEY, None)
    else:
        os.environ[crypto.ENV_KEY] = old
    crypto._cache.clear()


def _cred(db, provider: str, username: str) -> PortalCredential:
    if db.get(Tenant, TENANT) is None:
        db.add(Tenant(id=TENANT, name=TENANT, contact_email=f"{TENANT}@example.com",
                      tenant_key=f"key_{TENANT}", active=True, is_demo=True))
        db.flush()
    row = cc.upsert_credential(db, TENANT, provider, username, "pw", enable=True)
    db.flush()
    row.harvest_fails, row.last_harvest_ok, row.last_harvest_at = 0, None, None
    db.flush()
    return row


def _run(db, cred, status: str) -> None:
    cc.record_health(db, cred, ok=(status == "ok"), status=status, started_at=now(),
                     fresh_login=False, rows_written=1 if status == "ok" else 0)
    db.flush()


def _streak(db, cred) -> int:
    return sch.failure_streaks(db, [cred]).get(
        (cred.tenant_id, cred.provider, cred.username_lc), 0)


# ── failure streak backoff ──────────────────────────────────────────────────

def test_backoff_schedule_doubles_then_caps_and_never_stops():
    base = sch.INVERTER_DUE
    assert sch.failure_backoff("chint", 0) == base
    assert sch.failure_backoff("chint", 1) == base           # one quick retry
    assert sch.failure_backoff("chint", 2) == base * 2
    assert sch.failure_backoff("chint", 3) == base * 4
    assert sch.failure_backoff("chint", 50) == sch.FAILURE_BACKOFF_CAP
    # Utilities already run slower than the cap — untouched.
    assert sch.failure_backoff("gmp", 50) == sch.UTILITY_DUE


def test_streak_counts_consecutive_failures_and_an_ok_resets_it():
    with SessionLocal() as db:
        c = _cred(db, "chint", "streak@example.com")
        assert _streak(db, c) == 0
        for _ in range(3):
            _run(db, c, "scrape_failed")
        assert _streak(db, c) == 3
        _run(db, c, "ok")
        assert _streak(db, c) == 0
        _run(db, c, "login_failed")                            # sso-resumed shape
        assert _streak(db, c) == 1


def test_streak_is_bounded_by_the_lookback():
    with SessionLocal() as db:
        c = _cred(db, "sma", "bounded@example.com")
        for _ in range(sch.FAILURE_STREAK_LOOKBACK + 5):
            _run(db, c, "login_failed")
        assert _streak(db, c) == sch.FAILURE_STREAK_LOOKBACK


def test_a_failing_capture_is_not_due_on_the_3_minute_loop():
    with SessionLocal() as db:
        c = _cred(db, "chint", "notdue@example.com")
        for _ in range(4):
            _run(db, c, "scrape_failed")
        streak = _streak(db, c)
        c.last_harvest_at = now() - sch.INVERTER_DUE - timedelta(seconds=5)
        assert sch._is_due(c, now(), 0, 0)                    # old behaviour: due
        assert not sch._is_due(c, now(), 0, streak)            # now: backed off
        c.last_harvest_at = now() - sch.FAILURE_BACKOFF_CAP - timedelta(seconds=5)
        assert sch._is_due(c, now(), 0, streak)                # but never stopped


def test_a_healthy_capture_keeps_the_5_minute_sla():
    with SessionLocal() as db:
        c = _cred(db, "fronius", "healthy@example.com")
        for _ in range(3):
            _run(db, c, "ok")
        c.last_harvest_at = now() - sch.INVERTER_DUE - timedelta(seconds=5)
        assert sch._is_due(c, now(), 0, _streak(db, c))


# ── browser only while busy ─────────────────────────────────────────────────

class _FakeFarm:
    opened = 0
    closed = 0

    async def __aenter__(self):
        type(self).opened += 1
        return self

    async def __aexit__(self, *exc):
        type(self).closed += 1


def _drive(monkeypatch, due_per_tick, max_jobs=None):
    _FakeFarm.opened = _FakeFarm.closed = 0
    seq = iter(due_per_tick)
    monkeypatch.setattr(sch, "due_credentials", lambda: next(seq))
    ran = []

    async def fake_tick(farm, jobs=None):
        ran.append(list(jobs))
        return []

    monkeypatch.setattr(sch, "run_tick", fake_tick)
    monkeypatch.setattr(sch, "run_health_watchdogs", lambda: None)
    if max_jobs is not None:
        monkeypatch.setattr(sch, "FARM_MAX_JOBS", max_jobs)

    async def no_sleep(_):
        return None

    asyncio.run(sch.run_forever(farm_factory=_FakeFarm, sleep=no_sleep,
                                max_ticks=len(due_per_tick)))
    return ran


JOB = ("ten_x", "sma", "u@example.com")


def test_no_browser_is_launched_while_nothing_is_due(monkeypatch):
    _drive(monkeypatch, [[], [], []])
    assert _FakeFarm.opened == 0


def test_browser_closes_on_the_first_idle_tick(monkeypatch):
    ran = _drive(monkeypatch, [[JOB], [JOB], [], [JOB], []])
    assert ran == [[JOB], [JOB], [JOB]]
    assert _FakeFarm.opened == 2            # busy, busy, idle→close, busy, idle→close
    assert _FakeFarm.closed == 2


def test_a_busy_browser_is_recycled_after_the_job_budget(monkeypatch):
    _drive(monkeypatch, [[JOB]] * 5, max_jobs=2)
    assert _FakeFarm.opened == 3            # recycled after jobs 2 and 4, then final close
    assert _FakeFarm.closed == 3
