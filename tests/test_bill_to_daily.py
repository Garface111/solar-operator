"""Bill → daily-production transformer (api/jobs/bill_to_daily.py).

Proves: (1) a GMP bill's generation is prorated across its service days into
DailyGeneration rows with source='bill_prorate' so the frontend can show it;
(2) a real inverter/CSV reading is NEVER overwritten by the coarse bill estimate
(real data wins the (array,day) slot); (3) re-running is idempotent.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_billdaily_test")

from datetime import date, datetime
import secrets

from sqlalchemy import select
from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, DailyGeneration)
from api.jobs import bill_to_daily


def _seed():
    tid = "ten_b2d_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="B2D",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        a = Array(tenant_id=tid, name="Starlake", region="VT")
        db.add(a); db.flush()
        acc = UtilityAccount(tenant_id=tid, provider="gmp", account_number="999", array_id=a.id)
        db.add(acc); db.flush()
        # A 10-day bill, 1000 kWh → 100 kWh/day prorated.
        db.add(Bill(tenant_id=tid, account_id=acc.id,
                    period_start=datetime(2026, 5, 1), period_end=datetime(2026, 5, 10),
                    kwh_generated=1000, total_cost=210.0))
        # A REAL inverter reading on May 5 — must NOT be overwritten.
        db.add(DailyGeneration(tenant_id=tid, array_id=a.id, day=date(2026, 5, 5),
                               kwh=137.0, source="extension_pull"))
        db.commit()
        return tid, a.id


def test_bill_prorates_and_respects_real_data():
    tid, aid = _seed()
    r = bill_to_daily.transform_tenant_bills(tid)
    assert r["bills_seen"] == 1
    # 10-day bill, but May 5 already had a real reading → 9 written, 1 skipped.
    assert r["days_written"] == 9, r
    assert r["days_skipped_real"] == 1, r
    with SessionLocal() as db:
        rows = {d: (kwh, src) for d, kwh, src in db.execute(
            select(DailyGeneration.day, DailyGeneration.kwh, DailyGeneration.source)
            .where(DailyGeneration.array_id == aid)).all()}
        # May 5 keeps the REAL inverter value + source.
        assert rows[date(2026, 5, 5)] == (137.0, "extension_pull")
        # A prorated day = 100 kWh, source bill_prorate.
        assert abs(rows[date(2026, 5, 1)][0] - 100.0) < 0.01
        assert rows[date(2026, 5, 1)][1] == "bill_prorate"
        # Total days present = 10 (9 prorated + 1 real).
        assert len(rows) == 10


def test_idempotent_rerun():
    tid, aid = _seed()
    bill_to_daily.transform_tenant_bills(tid)
    r2 = bill_to_daily.transform_tenant_bills(tid)
    # Second run writes nothing new (same values) and never clobbers the real day.
    assert r2["days_written"] == 0, r2
    assert r2["days_skipped_real"] == 1, r2
    with SessionLocal() as db:
        n = db.execute(select(DailyGeneration.day).where(
            DailyGeneration.array_id == aid)).all()
        assert len(n) == 10  # no duplicate rows
