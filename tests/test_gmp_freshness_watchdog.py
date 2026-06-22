"""GMP freshness watchdog — flag active GMP tenants that stopped capturing.

GMP data only refreshes when the extension runs in the owner's browser; a stale
tenant means we'd bill/report from frozen data. The watchdog must flag a tenant
whose newest capture is >= STALE_DAYS old, leave fresh ones alone, and skip
inactive tenants.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_freshness_test")

from datetime import datetime, timedelta
import secrets

from api.db import SessionLocal
from api.models import Tenant, UtilityAccount, Array, Bill
from api.jobs.gmp_freshness_watchdog import (
    scan_stale_gmp_captures, tenant_gmp_freshness)


def _mk(*, days_ago, active=True):
    tid = "ten_fresh_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="F",
                      contact_email=f"{tid}@e.com", active=active, product="array_operator"))
        db.flush()
        a = Array(tenant_id=tid, name="A" + secrets.token_hex(2))
        db.add(a); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider="gmp",
                              account_number="G" + secrets.token_hex(3))
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2026, 5, 1), period_end=datetime(2026, 5, 31),
                    kwh_generated=1000,
                    pulled_at=datetime.utcnow() - timedelta(days=days_ago)))
        db.commit()
        return tid


def test_scan_flags_stale_not_fresh():
    fresh = _mk(days_ago=1)
    stale = _mk(days_ago=30)
    res = scan_stale_gmp_captures(stale_days=7)
    flagged = {s["tenant"] for s in res["stale"]}
    assert f"{stale}@e.com" in flagged          # 30d → flagged
    assert f"{fresh}@e.com" not in flagged       # 1d → fresh


def test_tenant_freshness_days():
    tid = _mk(days_ago=10)
    with SessionLocal() as db:
        f = tenant_gmp_freshness(db, tid)
    assert f is not None
    assert f["accounts"] == 1
    assert f["days_stale"] == 10


def test_inactive_tenant_skipped():
    tid = _mk(days_ago=30, active=False)
    res = scan_stale_gmp_captures(stale_days=7)
    assert f"{tid}@e.com" not in {s["tenant"] for s in res["stale"]}
