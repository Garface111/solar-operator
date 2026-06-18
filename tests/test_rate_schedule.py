"""Tests for the auto-applied blended rate schedule (derived from captured bills)."""
from __future__ import annotations

import secrets
from datetime import date, datetime

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, Bill, RateSchedule
from api import rate_schedule as rs


def _bill_raw(consumed_kwh, charge_usd):
    """A minimal GMP-shaped raw_json with one segment: consumed kWh + a positive
    NET energy charge → blended rate = charge/consumed."""
    return {"billSegments": [{"segmentLineItems": [
        {"unitOfMeasure": "KWH", "unitCode": "CONSUMED", "unitCount": consumed_kwh, "dollarAmount": 0.0},
        {"unitOfMeasure": "KWH", "unitCode": "NET", "unitCount": consumed_kwh, "dollarAmount": charge_usd},
    ]}]}


def test_blended_rate_from_bill_and_guard():
    # 1000 kWh, $200 → $0.20/kWh
    assert abs(rs.blended_rate_from_bill(_bill_raw(1000, 200.0)) - 0.20) < 1e-9
    # out-of-band rate rejected (parse noise guard)
    assert rs.blended_rate_from_bill(_bill_raw(1000, 5.0)) is None    # 0.005 too low
    assert rs.blended_rate_from_bill(_bill_raw(1000, 9999.0)) is None  # too high
    assert rs.blended_rate_from_bill({}) is None


def test_age_bucket():
    today = date(2026, 6, 1)
    assert rs.array_age_bucket(datetime(2020, 1, 1), today) == "le11"   # 6 yrs
    assert rs.array_age_bucket(datetime(2010, 1, 1), today) == "gt11"   # 16 yrs
    assert rs.array_age_bucket(None, today) == "le11"                   # unknown → le11


def _seed_tenant_array(provider="gmp", region="central", fc=None):
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Rate Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="RC", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="RA", client_id=c.id, fuel_type="solar",
                    region=region, first_connect_date=fc); db.add(arr); db.flush()
        ua = UtilityAccount(tenant_id=tid, array_id=arr.id, provider=provider,
                            account_number="A1", enabled=True); db.add(ua)
        db.commit()
        return tid, arr.id, ua.id


def test_derive_and_resolve_from_bills():
    tid, aid, acct_id = _seed_tenant_array(provider="gmp", fc=datetime(2021, 1, 1))
    # seed 10 bills in the 2024-2026 window, blended ~ $0.19-0.21
    with SessionLocal() as db:
        for i in range(10):
            db.add(Bill(tenant_id=tid, account_id=acct_id,
                        period_start=datetime(2024, 6, 1), period_end=datetime(2024, 6, 30),
                        kwh_generated=100, parse_status="parsed",
                        raw_json=_bill_raw(1000, 190 + i)))   # 0.190..0.199
        db.commit()

    with SessionLocal() as db:
        d = rs.derive_blended_rate_from_bills(
            db, utility="gmp", effective_start=date(2024, 1, 1),
            effective_end=date(2026, 1, 1), age_bucket="le11", min_samples=8)
    assert d is not None
    assert 0.19 <= d.rate <= 0.20
    assert d.sample_size == 10

    # refresh writes a RateSchedule row, then the resolver picks it up.
    with SessionLocal() as db:
        summary = rs.refresh_rate_schedule(db, utilities=["gmp"], min_samples=8)
    assert summary["written"] >= 1

    with SessionLocal() as db:
        arr = db.get(Array, aid)
        res = rs.resolve_net_rate(db, provider="gmp", region="central",
                                  first_connect_date=arr.first_connect_date,
                                  period_end=date(2024, 6, 30))
    assert res.source in ("schedule", "schedule_provisional")
    assert 0.19 <= res.rate <= 0.20
    assert "GMP" in res.note


def test_resolve_falls_back_to_vt_default_when_no_schedule():
    tid, aid, acct_id = _seed_tenant_array(provider="gmp", fc=datetime(2022, 1, 1))
    with SessionLocal() as db:
        res = rs.resolve_net_rate(db, provider="gmp", region="central",
                                  first_connect_date=datetime(2022, 1, 1),
                                  period_end=date(2026, 6, 30))
    # No rows seeded for this isolated tenant's window → VT default, honest source.
    assert res.source == "vt_default"
    assert res.rate > 0
