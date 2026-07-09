"""gmp_daily_backfill must retry a transient window failure with backoff before
giving up, and must be HONEST (status "partial", not "ok") when a window stays
stuck after retrying -- not silently under-fill an account's history while
reporting it as fully successful.

Ford, 2026-07-08: "find every instance of us intentionally sabotaging our own
reliability and fix it." A single non-200 mid-walk used to stop the backfill
immediately and leave summary["status"] at its default "ok", with no
documented vendor rate-limit reason ("avoid hammering" cited no real
constraint) -- exactly that class of bug.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from api.db import SessionLocal
from api.jobs import gmp_daily_backfill as backfill
from api.models import Tenant, UtilityAccount


def _mk_gmp_account() -> int:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="GMP Backfill Retry Test", contact_email=f"{tid}@t.t",
                      tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True))
        db.flush()
        acct = UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number="acct-" + secrets.token_hex(3), enabled=True)
        db.add(acct)
        db.commit()
        return acct.id


def _fake_session():
    return SimpleNamespace(api_token="tok", refresh_token=None,
                           expires_at=datetime.utcnow() + timedelta(days=30))


def _empty_parsed():
    return {"by_day": {}, "row_count": 0, "interval_min": None, "interval_max": None,
            "service_agreements": [], "unit": None}


def test_transient_window_failure_recovers_on_retry(monkeypatch):
    """A window that fails once then succeeds must NOT be treated as a
    permanent wall -- the walk continues and status stays ok."""
    monkeypatch.setattr(backfill, "_usable_session", lambda db, account: _fake_session())
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)
    account_id = _mk_gmp_account()

    calls = {"n": 0}

    def _fake_fetch(db, sess, account, jwt, start, end):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", _empty_parsed(), 500, False   # first attempt: transient failure
        if calls["n"] == 2:
            # recovers on retry with real data, then the next window (walking
            # further back) is below the meter's floor -> clean stop.
            parsed = _empty_parsed()
            return "ok-csv", parsed, 200, False
        return "", _empty_parsed(), 404, False

    monkeypatch.setattr(backfill, "_fetch_one_window", _fake_fetch)
    out = backfill.backfill_account(None, account_id, max_windows=5)
    assert out["status"] == "ok"
    assert calls["n"] >= 2   # the retry actually happened
    assert out["windows_fetched"] == 1   # the recovered window's data was kept


def test_persistent_window_failure_is_marked_partial_not_ok(monkeypatch):
    """A window that NEVER recovers must mark the account 'partial' (visible,
    honest) rather than 'ok' (silently incomplete) -- and must have actually
    retried WINDOW_ERROR_RETRIES times before giving up."""
    monkeypatch.setattr(backfill, "_usable_session", lambda db, account: _fake_session())
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)
    account_id = _mk_gmp_account()

    calls = {"n": 0}

    def _fake_fetch(db, sess, account, jwt, start, end):
        calls["n"] += 1
        return "", _empty_parsed(), 500, False   # always fails

    monkeypatch.setattr(backfill, "_fetch_one_window", _fake_fetch)
    out = backfill.backfill_account(None, account_id, max_windows=5)
    assert out["status"] == "partial"
    assert any("HTTP 500" in e for e in out["errors"])
    assert any("retries" in e for e in out["errors"])
    # Initial attempt + WINDOW_ERROR_RETRIES retries, all exhausted.
    assert calls["n"] == 1 + backfill.WINDOW_ERROR_RETRIES
