"""Multi-array offtaker billing: an offtaker owning a share of several arrays
gets ONE combined invoice summing (each array's period kWh × its pct), with a
per-array breakdown. Back-compat: single-array subs are unchanged."""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_multiarray_test")

from datetime import date
import pytest

from api.db import SessionLocal
from api.models import Tenant, Array, DailyGeneration
from api.billing import delivery
import secrets as _secrets


class _Sub:
    """Minimal stand-in for a BillingReportSubscription (manual path)."""
    def __init__(self, **kw):
        self.source_workbook = None
        self.customer_name = "Paul Bozuwa"
        self.client_email = None
        self.tenant_id = None
        self.array_id = None
        self.allocation_pct = None
        self.array_allocations = None
        self.rate_per_kwh = None
        self.discount_pct = None
        self.net_rate_per_kwh = None
        for k, v in kw.items():
            setattr(self, k, v)


def _seed_two_arrays():
    tid = "ten_multiarr_" + _secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="MA Test",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        a1 = Array(tenant_id=tid, name="Maple Grove", region="VT")
        a2 = Array(tenant_id=tid, name="Fair Haven", region="VT")
        db.add_all([a1, a2]); db.flush()
        # June 2026: A1 = 1000 kWh, A2 = 2000 kWh (4 days each, summing to those).
        for d in range(1, 5):
            db.add(DailyGeneration(tenant_id=tid, array_id=a1.id, day=date(2026, 6, d),
                                   kwh=250.0, source="csv"))
            db.add(DailyGeneration(tenant_id=tid, array_id=a2.id, day=date(2026, 6, d),
                                   kwh=500.0, source="csv"))
        db.commit()
        return tid, a1.id, a2.id


def test_multi_array_sums_into_one_invoice():
    tid, a1, a2 = _seed_two_arrays()
    # Offtaker owns 25% of A1 (1000 kWh) + 50% of A2 (2000 kWh) = 250 + 1000 = 1250.
    sub = _Sub(tenant_id=tid, array_allocations=[
        {"array_id": a1, "allocation_pct": 0.25},
        {"array_id": a2, "allocation_pct": 0.50},
    ])
    m = delivery.build_manual_match(sub)
    assert m.matched
    inv = m.computed_invoice
    assert abs(inv["kwh"] - 1250.0) < 0.5, inv["kwh"]
    bd = inv["array_breakdown"]
    assert len(bd) == 2
    by_name = {b["array_name"]: b for b in bd}
    assert abs(by_name["Maple Grove"]["customer_kwh"] - 250.0) < 0.5
    assert abs(by_name["Fair Haven"]["customer_kwh"] - 1000.0) < 0.5
    # combined total kWh = sum of the per-array customer shares
    assert abs(sum(b["customer_kwh"] for b in bd) - inv["kwh"]) < 0.5


def test_single_array_back_compat():
    tid, a1, a2 = _seed_two_arrays()
    sub = _Sub(tenant_id=tid, array_id=a1, allocation_pct=0.25)  # legacy single-array
    m = delivery.build_manual_match(sub)
    assert m.matched
    # 25% of A1's 1000 kWh = 250; no breakdown for single array.
    assert abs(m.computed_invoice["kwh"] - 250.0) < 0.5
    assert not m.computed_invoice.get("array_breakdown")
