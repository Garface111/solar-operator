"""Real transactional capture races must preserve every distinct source."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier
import secrets

import pytest
from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount, GmpUsageRaw, GmpUsageRawVersion
from api.source_artifacts import get_artifact, read_raw_csv
from api.jobs.gmp_daily_backfill import _persist_window, _record_404, _empty_parsed

WS, WE = date(2026, 1, 1), date(2026, 2, 1)


@pytest.fixture
def account_id():
    tid = "race_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Raw race", tenant_key=secrets.token_hex(16),
                      contact_email=tid + "@example.test"))
        arr = Array(tenant_id=tid, name="Race")
        db.add(arr)
        db.flush()
        account = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                                 account_number="race-account")
        db.add(account)
        db.commit()
        return account.id


def test_simultaneous_first_captures_preserve_both_versions(account_id):
    barrier = Barrier(2)
    payloads = ["header\nfirst source", "header\nsecond source"]
    def capture(i):
        with SessionLocal() as db:
            account = db.get(UtilityAccount, account_id)
            barrier.wait(timeout=10)
            parsed = _empty_parsed()
            parsed.update(row_count=1, interval_min=WS, interval_max=WS,
                          by_day={WS: {"kwh": i + 1, "intervals": 1}})
            _persist_window(db, account, WS, WE, payloads[i], parsed,
                            captured_at=datetime(2026, 3, i + 1))
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(capture, range(2)))
    with SessionLocal() as db:
        raw = db.execute(select(GmpUsageRaw).where(
            GmpUsageRaw.account_id == account_id)).scalar_one()
        versions = db.execute(select(GmpUsageRawVersion).where(
            GmpUsageRawVersion.raw_id == raw.id)).scalars().all()
        assert {get_artifact(db, raw.tenant_id, v.artifact_id).decode() for v in versions} == set(payloads)
        assert {v.captured_at for v in versions} == {datetime(2026, 3, 1), datetime(2026, 3, 2)}
        assert read_raw_csv(db, raw) in payloads


def test_simultaneous_404_and_first_success_keep_source(account_id):
    barrier = Barrier(2)
    def capture(missing):
        with SessionLocal() as db:
            account = db.get(UtilityAccount, account_id)
            barrier.wait(timeout=10)
            if missing:
                _record_404(db, account, WS, WE)
            else:
                _persist_window(db, account, WS, WE, "retained source", _empty_parsed())
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(capture, [False, True]))
    with SessionLocal() as db:
        raw = db.execute(select(GmpUsageRaw).where(
            GmpUsageRaw.account_id == account_id)).scalar_one()
        assert raw.http_status == 200
        assert read_raw_csv(db, raw) == "retained source"


def test_missing_response_does_not_erase_last_success(account_id):
    with SessionLocal() as db:
        account = db.get(UtilityAccount, account_id)
        _persist_window(db, account, WS, WE, "retained source", _empty_parsed(),
                        captured_at=datetime(2026, 3, 1))
        db.commit()
        assert _persist_window(db, account, WS, WE, None, _empty_parsed()) == (0, 0)
        db.commit()
        raw = db.execute(select(GmpUsageRaw).where(
            GmpUsageRaw.account_id == account_id)).scalar_one()
        assert read_raw_csv(db, raw) == "retained source"
        assert raw.fetched_at == datetime(2026, 3, 1)
