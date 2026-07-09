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

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, UtilityAccount, Array, Bill, InverterAlertState
from api.jobs.gmp_freshness_watchdog import (
    scan_stale_gmp_captures, tenant_gmp_freshness, run_gmp_freshness_watchdog,
    _REALERT_DAYS)


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


def _refresh_bill(tid, days_ago):
    with SessionLocal() as db:
        acct = db.execute(select(UtilityAccount.id).where(
            UtilityAccount.tenant_id == tid)).scalar_one()
        bill = db.execute(select(Bill).where(Bill.account_id == acct)).scalars().first()
        bill.pulled_at = datetime.utcnow() - timedelta(days=days_ago)
        db.commit()


def test_watchdog_alerts_once_then_dedups_daily_then_clears_on_recovery(monkeypatch):
    # The core of the weekly->daily fix: a still-stale tenant must NOT re-alert every
    # daily run -- only once per _REALERT_DAYS -- and must clear when it recovers.
    # Scope every assertion to THIS test's tenant via the authoritative per-tenant
    # `alerted` list + its own incident row (the aggregate email truncates to the
    # top 40, and this shared test DB carries many other leaked stale tenants).
    monkeypatch.setattr("api.jobs.gmp_freshness_watchdog.send_internal_alert",
                        lambda subject, body: None)
    tid = _mk(days_ago=30)
    key = f"gmp_freshness:{tid}"

    def _state():
        with SessionLocal() as db:
            return db.execute(select(InverterAlertState).where(
                InverterAlertState.incident_key == key)).scalar_one_or_none()

    # First daily run: THIS tenant alerts once and gets an incident row.
    out1 = run_gmp_freshness_watchdog(stale_days=7)
    assert tid in out1["alerted"]
    assert _state() is not None and _state().last_alerted_at is not None

    # Second daily run, still stale, inside the re-alert window: deduped for this tenant.
    out2 = run_gmp_freshness_watchdog(stale_days=7)
    assert tid not in out2["alerted"]

    # Backdate the incident past the re-alert window -> it alerts AGAIN (never silent
    # forever on a persistent freeze).
    with SessionLocal() as db:
        st = db.execute(select(InverterAlertState).where(
            InverterAlertState.incident_key == key)).scalar_one()
        st.last_alerted_at = datetime.utcnow() - timedelta(days=_REALERT_DAYS + 1)
        db.commit()
    out3 = run_gmp_freshness_watchdog(stale_days=7)
    assert tid in out3["alerted"]

    # Recovers (fresh capture) -> incident clears, no alert.
    _refresh_bill(tid, days_ago=0)
    out4 = run_gmp_freshness_watchdog(stale_days=7)
    assert tid not in out4["alerted"]
    assert _state() is None
