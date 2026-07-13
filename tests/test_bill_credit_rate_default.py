"""Master solar credit rate defaults to the fleet's utility-bill EXCESS credit
rate (not the Vermont tariff constant)."""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, UtilityAccount, Bill, Array
from api.rate_schedule import tenant_bill_credit_rate, excess_credit_rate_from_bill


def _tenant():
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Bill Rate Op", contact_email=f"{tid}@t.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _gmp_bill_raw(rate=0.18398, kwh=100.0):
    return {
        "billSegments": [{
            "segmentLineItems": [
                {"unitOfMeasure": "KWH", "unitCode": "EXCESS",
                 "unitCount": kwh, "dollarAmount": -round(rate * kwh, 2)},
                # group-shared $0 excess must not dilute the rate
                {"unitOfMeasure": "KWH", "unitCode": "EXCESSO",
                 "unitCount": 500.0, "dollarAmount": 0},
            ]
        }]
    }


def test_excess_credit_rate_ignores_zero_shared_line():
    r = excess_credit_rate_from_bill(_gmp_bill_raw(0.18398, 50))
    assert r is not None and abs(r - 0.18398) < 1e-4


def test_tenant_bill_credit_rate_median(client):
    tid, auth = _tenant()
    pe = date.today().replace(day=1) - timedelta(days=1)
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Host Array", fuel_type="solar")
        db.add(arr); db.flush()
        ua = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                            account_number="GMP-RATE-1", nickname="Host")
        db.add(ua); db.flush()
        # Three bills at different rates → median is the middle one.
        for rate in (0.17, 0.20, 0.18):
            db.add(Bill(
                tenant_id=tid, account_id=ua.id, bill_date=pe,
                period_start=pe.replace(day=1), period_end=pe,
                kwh_generated=1000.0, document_number="D-" + secrets.token_hex(3),
                raw_json=_gmp_bill_raw(rate, 80.0),
            ))
        db.commit()
        meta = tenant_bill_credit_rate(db, tid)
    assert meta["source"] == "utility_bills"
    assert meta["sample_size"] == 3
    assert abs(meta["rate"] - 0.18) < 1e-4

    r = client.get("/v1/array-operator/billing/global-rate",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["default_net_rate_per_kwh"] is None
    assert body["effective_net_rate_source"] == "utility_bills"
    assert abs(body["effective_net_rate_per_kwh"] - 0.18) < 1e-4
    assert abs(body["bill_credit_rate_per_kwh"] - 0.18) < 1e-4
    assert body["bill_credit_rate_sample"] == 3
