"""_fleet_credit_rate must NOT let non-production (demo/synthetic) tenants poison
the cross-tenant reference median that prices REAL banked-month offtaker invoices.

Regression for the 2026-07-17 finding: seed_demo.py rigs GMP bills at 0.140–0.176
$/kWh under `ten_demo_realistic`, which carries is_demo=False — so neither the
(absent) filter nor the codebase-standard `Tenant.is_demo` filter excluded it, and
those low synthetic rates dragged the GMP fleet median down under real invoices.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_fleet_demo_excl_test")

from datetime import datetime
import secrets

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount, Bill
from api.rate_schedule import (
    _fleet_credit_rate, array_age_bucket, SYNTHETIC_TENANT_IDS,
)

# Unique provider so ONLY this test's bills match the fleet query — isolates the
# assertion from any other GMP bills seeded by sibling tests in the shared DB.
PROV = "zzz_demo_excl_" + secrets.token_hex(3)
FIRST_CONNECT = datetime(2020, 5, 1)   # recent → a single, shared age bucket


def _seed(db, tid, *, rate, n, is_demo=False):
    db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name=tid,
                  contact_email=f"{tid}@e.com", active=True,
                  product="array_operator", is_demo=is_demo))
    db.flush()
    a = Array(tenant_id=tid, name=tid + " arr", region="VT",
              first_connect_date=FIRST_CONNECT)
    db.add(a); db.flush()
    acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider=PROV,
                          account_number="A" + secrets.token_hex(3))
    db.add(acct); db.flush()
    for m in range(n):
        excess = 1000.0
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2025, (m % 12) + 1, 1),
                    period_end=datetime(2025, (m % 12) + 1, 28),
                    kwh_generated=1000, kwh_sent_to_grid=excess,
                    solar_credit_usd=round(excess * rate, 2)))
    return a.first_connect_date


def test_denylist_covers_the_known_mismarked_tenants():
    assert "ten_demo_realistic" in SYNTHETIC_TENANT_IDS
    assert "ten_ford_demo_100" in SYNTHETIC_TENANT_IDS


def test_fleet_median_excludes_demo_and_synthetic_tenants():
    real = "ten_real_" + secrets.token_hex(3)
    demo_marked = "ten_demomark_" + secrets.token_hex(3)
    with SessionLocal() as db:
        fc = _seed(db, real, rate=0.30, n=8)                       # REAL → counts
        _seed(db, demo_marked, rate=0.14, n=8, is_demo=True)       # is_demo → excluded
        _seed(db, "ten_demo_realistic", rate=0.14, n=8)            # denylist → excluded
        db.commit()
        bucket = array_age_bucket(fc, datetime(2025, 6, 28).date())
        med = _fleet_credit_rate(db, provider=PROV, age_bucket=bucket, min_samples=8)
    # Only the 8 real bills @ 0.30 survive. If demo leaked in, the median of
    # 8×0.30 + 16×0.14 would be 0.14 — the exact underpricing bug.
    assert med is not None, "real tenant alone has 8 samples (== min_samples)"
    assert abs(med - 0.30) < 1e-6, f"demo/synthetic leaked into fleet median: {med}"


def test_all_demo_means_no_fleet_rate():
    # If a provider+bucket has ONLY synthetic bills, the median is None (too few
    # real samples) — the resolver then correctly falls to DEFAULT, never a fake.
    prov_only_demo = "zzz_onlydemo_" + secrets.token_hex(3)
    tid = "ten_demo_realistic"
    with SessionLocal() as db:
        db.add(Tenant(id=tid + "_x", tenant_key=secrets.token_hex(8), name="d",
                      contact_email="d@e.com", active=True,
                      product="array_operator", is_demo=True))
        db.flush()
        a = Array(tenant_id=tid + "_x", name="d arr", region="VT",
                  first_connect_date=FIRST_CONNECT)
        db.add(a); db.flush()
        acct = UtilityAccount(tenant_id=tid + "_x", array_id=a.id,
                              provider=prov_only_demo,
                              account_number="A" + secrets.token_hex(3))
        db.add(acct); db.flush()
        for m in range(10):
            db.add(Bill(tenant_id=tid + "_x", account_id=acct.id,
                        period_start=datetime(2025, (m % 12) + 1, 1),
                        period_end=datetime(2025, (m % 12) + 1, 28),
                        kwh_generated=1000, kwh_sent_to_grid=1000.0,
                        solar_credit_usd=140.0))
        db.commit()
        bucket = array_age_bucket(FIRST_CONNECT, datetime(2025, 6, 28).date())
        med = _fleet_credit_rate(db, provider=prov_only_demo, age_bucket=bucket)
    assert med is None, f"synthetic-only cell should yield no fleet rate, got {med}"
