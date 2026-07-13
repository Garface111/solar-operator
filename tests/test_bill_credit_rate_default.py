"""Master solar credit rate: blank = per-offtaker utility bill rate;
set = fleet override. Never a fleet median."""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, UtilityAccount, Bill, Array, BillingReportSubscription, Client
from api.rate_schedule import excess_credit_rate_from_bill
from api.billing.delivery import resolve_discount_pricing


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
                {"unitOfMeasure": "KWH", "unitCode": "EXCESSO",
                 "unitCount": 500.0, "dollarAmount": 0},
            ]
        }]
    }


def test_excess_credit_rate_ignores_zero_shared_line():
    r = excess_credit_rate_from_bill(_gmp_bill_raw(0.18398, 50))
    assert r is not None and abs(r - 0.18398) < 1e-4


def test_global_rate_blank_means_per_offtaker_bill(client):
    """GET /global-rate with no master override → source per_offtaker_bill, no single rate."""
    tid, auth = _tenant()
    r = client.get("/v1/array-operator/billing/global-rate",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["default_net_rate_per_kwh"] is None
    assert body["effective_net_rate_source"] == "per_offtaker_bill"
    assert body["effective_net_rate_per_kwh"] is None
    assert "own" in (body.get("effective_net_rate_note") or "").lower() \
        or "bill" in (body.get("effective_net_rate_note") or "").lower()


def test_global_rate_set_is_fleet_override(client):
    tid, auth = _tenant()
    r = client.put("/v1/array-operator/billing/global-rate",
                   json={"default_net_rate_per_kwh": 0.22},
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    r = client.get("/v1/array-operator/billing/global-rate",
                   headers={"Authorization": auth})
    body = r.json()
    assert body["default_net_rate_per_kwh"] == 0.22
    assert body["effective_net_rate_source"] == "global"
    assert abs(body["effective_net_rate_per_kwh"] - 0.22) < 1e-9


def test_resolve_pricing_tenant_net_rate_exposed():
    """resolve_discount_pricing carries tenant_net_rate so bill path can apply master."""
    tid, _ = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        t.default_net_rate_per_kwh = 0.19
        db.commit()
        # Minimal sub-like object
        class _S:
            net_rate_per_kwh = None
            discount_pct = None
            rate_per_kwh = None
            array_id = None
            tenant_id = tid
        p = resolve_discount_pricing(_S())
        assert p["net_source"] == "global"
        assert abs(p["tenant_net_rate"] - 0.19) < 1e-9
        assert abs(p["net_rate"] - 0.19) < 1e-9
