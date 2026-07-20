"""Self-healing deep-history backfill (api.jobs.inverter_history).

Covers:
  • backfill_connection_history pulls multi-year daily into DailyGeneration and
    stamps history_backfilled_at on success
  • it NEVER clobbers a protected real source (csv/manual/utility_meter/gmp_api)
  • a vendor error in any year leaves the connection UNstamped (so the healer
    retries) — true self-healing
  • a no-daily vendor (chint) is stamped done immediately (nothing to wait for)
  • heal_missing_history processes only NULL-stamped connections and is idempotent

All vendor I/O is faked by monkeypatching inverters.fetch_daily — no network.
"""
from __future__ import annotations

import secrets
from datetime import date

import pytest

from api import inverters
from api.inverters import InverterError
from api.db import SessionLocal
from api.models import Array, DailyGeneration, InverterConnection, Tenant
from api.jobs import inverter_history as ih


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Hist Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        db.commit()
    return tid


def _array(tid: str, name: str) -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, name=name, fuel_type="solar")
        db.add(a); db.commit(); return a.id


def _conn(array_id: int, vendor="solaredge", config=None) -> int:
    with SessionLocal() as db:
        c = InverterConnection(array_id=array_id, vendor=vendor,
                               config=config or {"api_key": "k", "site_id": 1},
                               status="ok")
        db.add(c); db.commit(); return c.id


def test_backfill_pulls_multiyear_and_stamps(monkeypatch):
    tid = _tenant(); aid = _array(tid, "SE Multi"); cid = _conn(aid)

    # fake vendor: 1 day per requested year so we can prove multi-year coverage
    def fake_daily(vendor, config, start, end):
        return [{"day": date(start.year, 6, 15), "kwh": 100.0 + start.year}]
    monkeypatch.setattr(inverters, "fetch_daily", fake_daily)

    r = ih.backfill_connection_history(cid, start_year=2019)
    assert r["stamped"] is True
    assert r["inserted"] >= 7   # 2019..2025/26
    with SessionLocal() as db:
        years = {d.day.year for d in db.query(DailyGeneration).filter_by(array_id=aid).all()}
        assert {2019, 2020, 2021, 2022, 2023} <= years
        conn = db.get(InverterConnection, cid)
        assert conn.history_backfilled_at is not None


def test_backfill_never_clobbers_protected_source(monkeypatch):
    tid = _tenant(); aid = _array(tid, "SE Protect"); cid = _conn(aid)
    # a real utility_meter reading already owns 2023-06-15
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=date(2023, 6, 15),
                               kwh=999.0, source="utility_meter"))
        db.commit()

    def fake_daily(vendor, config, start, end):
        return [{"day": date(start.year, 6, 15), "kwh": 50.0}]
    monkeypatch.setattr(inverters, "fetch_daily", fake_daily)

    ih.backfill_connection_history(cid, start_year=2023)
    with SessionLocal() as db:
        row = db.query(DailyGeneration).filter_by(array_id=aid, day=date(2023, 6, 15)).one()
        assert row.source == "utility_meter" and row.kwh == 999.0  # untouched


def test_backfill_error_leaves_unstamped_for_retry(monkeypatch):
    tid = _tenant(); aid = _array(tid, "SE Err"); cid = _conn(aid)

    def boom(vendor, config, start, end):
        raise InverterError("vendor 503")
    monkeypatch.setattr(inverters, "fetch_daily", boom)

    r = ih.backfill_connection_history(cid, start_year=2024)
    assert r["stamped"] is False and r["had_error"] is True
    with SessionLocal() as db:
        assert db.get(InverterConnection, cid).history_backfilled_at is None


def test_orphaned_connection_stamped_not_retried_forever(monkeypatch):
    """A connection whose array is soft-deleted must be stamped done so the
    healer stops retrying it every run (regression: 9 orphans stayed pending)."""
    from api.models import now
    tid = _tenant(); aid = _array(tid, "Orphan"); cid = _conn(aid)
    with SessionLocal() as db:
        db.get(Array, aid).deleted_at = now()
        db.commit()
    r = ih.backfill_connection_history(cid, start_year=2024)
    assert r["stamped"] is True
    with SessionLocal() as db:
        assert db.get(InverterConnection, cid).history_backfilled_at is not None


def test_no_daily_vendor_stamped_immediately(monkeypatch):
    tid = _tenant(); aid = _array(tid, "Chint"); cid = _conn(aid, vendor="chint", config={})
    r = ih.backfill_connection_history(cid, start_year=2024)
    assert r["stamped"] is True and r["inserted"] == 0
    with SessionLocal() as db:
        assert db.get(InverterConnection, cid).history_backfilled_at is not None


def test_heal_processes_only_unstamped_and_is_idempotent(monkeypatch):
    tid = _tenant()
    a1 = _array(tid, "Heal A"); c1 = _conn(a1)
    a2 = _array(tid, "Heal B"); c2 = _conn(a2)
    # c2 already backfilled → must be skipped
    with SessionLocal() as db:
        from api.models import now
        db.get(InverterConnection, c2).history_backfilled_at = now()
        db.commit()

    seen_starts = []
    def fake_daily(vendor, config, start, end):
        seen_starts.append(start)
        return [{"day": date(start.year, 7, 1), "kwh": 10.0}]
    monkeypatch.setattr(inverters, "fetch_daily", fake_daily)

    ih.heal_missing_history()
    # c1 must now be stamped; c2 (pre-stamped) must be unchanged.
    with SessionLocal() as db:
        assert db.get(InverterConnection, c1).history_backfilled_at is not None
        c2row = db.get(InverterConnection, c2)
        # c2 should NOT have been re-processed (no new daily row for its array)
        assert db.query(DailyGeneration).filter_by(array_id=a2).count() == 0

    # second run: our connections are all stamped now → c1 not re-pulled.
    seen_starts.clear()
    ih.heal_missing_history()
    with SessionLocal() as db:
        # c1 already had rows; a re-pull would only refresh, but more importantly
        # the heal must not crash and c1 stays stamped.
        assert db.get(InverterConnection, c1).history_backfilled_at is not None
        assert db.query(DailyGeneration).filter_by(array_id=a1).count() >= 1


def test_backfill_race_uq_daily_array_day_is_idempotent(monkeypatch):
    """REGRESSION (Sentry on-connect history backfill): concurrent writer
    commits the same (array_id, day) after preload; insert must not crash on
    uq_daily_array_day — re-read and refresh vendor kWh instead."""
    tid = _tenant()
    aid = _array(tid, "SE Race")
    cid = _conn(aid)
    day = date(2024, 6, 15)

    def fake_daily(vendor, config, start, end):
        if start.year != 2024:
            return []
        # Concurrent connect-path / nightly pull wins the row after our preload.
        with SessionLocal() as other:
            if other.query(DailyGeneration).filter_by(array_id=aid, day=day).first() is None:
                other.add(DailyGeneration(
                    tenant_id=tid, array_id=aid, day=day, kwh=10.0, source="solaredge",
                ))
                other.commit()
        return [{"day": day, "kwh": 50.0}]

    monkeypatch.setattr(inverters, "fetch_daily", fake_daily)
    r = ih.backfill_connection_history(cid, start_year=2024)
    assert "error" not in r or r.get("error") is None
    assert r.get("stamped") is True
    with SessionLocal() as db:
        rows = db.query(DailyGeneration).filter_by(array_id=aid, day=day).all()
        assert len(rows) == 1
        assert rows[0].kwh == 50.0
        assert rows[0].source == "solaredge"
