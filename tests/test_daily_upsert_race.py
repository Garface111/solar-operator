"""Race-safe daily upserts (Anna's uq_daily_array_day 500, 2026-07-04).

Reproduces the production failure deterministically: the capture handler reads
existing (array, day) rows, believes a row is missing, and inserts — but a
CONCURRENT capture committed the same row in between. Pre-fix that INSERT blew
up the whole request with IntegrityError(uq_daily_array_day); the helpers must
instead land the value via the same max-wins update the fresh-read path applies.
"""
from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import select

from api.array_owners import (
    _insert_daily_generation_race_safe,
    _insert_inverter_daily_race_safe,
)
from api.db import SessionLocal
from api.models import Array, DailyGeneration, Inverter, InverterDaily, Tenant


def _mk_fixture() -> tuple[str, int, int]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Race Test", contact_email=f"{tid}@x.test",
                      tenant_key="race_" + secrets.token_hex(8), plan="standard",
                      active=True))
        db.flush()
        arr = Array(tenant_id=tid, name="Race Arr", fuel_type="solar")
        db.add(arr)
        db.flush()
        inv = Inverter(tenant_id=tid, array_id=arr.id, name="R1",
                       vendor="solaredge", serial="RACE-1")
        db.add(inv)
        db.commit()
        return tid, arr.id, inv.id


def test_lost_race_falls_back_to_max_wins_update():
    tid, aid, _ = _mk_fixture()
    day = date(2026, 7, 4)
    # The "concurrent request" wins first, committing kwh=900 in its own session.
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=day, kwh=900.0,
                               source="extension_pull"))
        db.commit()
    # Our request read BEFORE that commit (row believed missing) and now inserts
    # a HIGHER value: must not raise, must land 1121.31 via max-wins update.
    with SessionLocal() as db:
        assert _insert_daily_generation_race_safe(
            db, tenant_id=tid, array_id=aid, day=day, kwh=1121.31) is True
        db.commit()
    with SessionLocal() as db:
        rows = db.execute(select(DailyGeneration).where(
            DailyGeneration.array_id == aid)).scalars().all()
        assert len(rows) == 1                     # never a duplicate
        assert rows[0].kwh == 1121.31


def test_lost_race_with_lower_value_never_regresses():
    tid, aid, _ = _mk_fixture()
    day = date(2026, 7, 4)
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=day, kwh=1200.0,
                               source="extension_pull"))
        db.commit()
    with SessionLocal() as db:
        # Loser carries a LOWER (earlier-in-day) value: no update, no crash.
        assert _insert_daily_generation_race_safe(
            db, tenant_id=tid, array_id=aid, day=day, kwh=800.0) is False
        db.commit()
    with SessionLocal() as db:
        row = db.execute(select(DailyGeneration).where(
            DailyGeneration.array_id == aid)).scalar_one()
        assert row.kwh == 1200.0                  # climbs through the day, never back


def test_clean_insert_still_works_and_session_stays_usable():
    tid, aid, _ = _mk_fixture()
    with SessionLocal() as db:
        assert _insert_daily_generation_race_safe(
            db, tenant_id=tid, array_id=aid, day=date(2026, 7, 3), kwh=500.0) is True
        # Session must remain fully usable after the savepoint path (the handler
        # keeps writing sibling sites/inverters in the same transaction).
        db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=date(2026, 7, 2),
                               kwh=400.0, source="extension_pull"))
        db.commit()
    with SessionLocal() as db:
        assert len(db.execute(select(DailyGeneration).where(
            DailyGeneration.array_id == aid)).scalars().all()) == 2


def test_inverter_daily_race_same_contract():
    tid, _, ivid = _mk_fixture()
    day = date(2026, 7, 4)
    with SessionLocal() as db:
        db.add(InverterDaily(tenant_id=tid, inverter_id=ivid, day=day, kwh=90.0,
                             source="extension_pull"))
        db.commit()
    with SessionLocal() as db:
        assert _insert_inverter_daily_race_safe(
            db, tenant_id=tid, inverter_id=ivid, day=day, kwh=120.0) is True
        db.commit()
    with SessionLocal() as db:
        rows = db.execute(select(InverterDaily).where(
            InverterDaily.inverter_id == ivid)).scalars().all()
        assert len(rows) == 1 and rows[0].kwh == 120.0


def test_utility_meter_lost_race_does_not_raise():
    """Sentry: /v1/array-owners/utility-meter-capture UniqueViolation
    uq_daily_array_day (ten_anna_800, array 2482, 2026-07-07). Concurrent
    capture already committed; our insert must SAVEPOINT-fallback, not 500."""
    tid, aid, _ = _mk_fixture()
    day = date(2026, 7, 7)
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=day, kwh=100.0,
                               source="utility_meter"))
        db.commit()
    with SessionLocal() as db:
        assert _insert_daily_generation_race_safe(
            db, tenant_id=tid, array_id=aid, day=day, kwh=437.76,
            source="utility_meter") is True
        db.commit()
    with SessionLocal() as db:
        rows = db.execute(select(DailyGeneration).where(
            DailyGeneration.array_id == aid)).scalars().all()
        assert len(rows) == 1
        assert rows[0].kwh == 437.76
        assert rows[0].source == "utility_meter"


def test_utility_meter_lost_race_never_clobbers_measured():
    tid, aid, _ = _mk_fixture()
    day = date(2026, 7, 7)
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=day, kwh=900.0,
                               source="solaredge"))
        db.commit()
    with SessionLocal() as db:
        assert _insert_daily_generation_race_safe(
            db, tenant_id=tid, array_id=aid, day=day, kwh=437.76,
            source="utility_meter") is False
        db.commit()
    with SessionLocal() as db:
        row = db.execute(select(DailyGeneration).where(
            DailyGeneration.array_id == aid)).scalar_one()
        assert row.kwh == 900.0
        assert row.source == "solaredge"
