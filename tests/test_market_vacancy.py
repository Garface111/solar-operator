"""Offtaker Exchange — vacancy computation + demand intake (v0).

Grounds the bill-side vacancy walker against the two calibration exemplars from
the plan, using GMP-shaped raw_json line items (the same _EXCESS_CODES the invoice
engine parses):

  * a Londonderry-like PERPETUAL BANKER — excess retained on the host, no $0
    group-shared line → ~100% vacant.
  * a Danville-like FULLY-ALLOCATED array — the whole pool on the $0 shared line
    → ~0% vacant.

Plus: registry-side estimator, confidence tiers, the tenant-scoped endpoint, the
demand-intake endpoints, and the synthetic-tenant guard.
"""
import secrets
from datetime import datetime, timedelta

from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill,
                        BillingReportSubscription, Client, now as _now)
from api.account import mint_session_for_tenant
from api.market_vacancy import (array_vacancy, tenant_vacancy,
                                split_excess_line_items, is_synthetic_tenant)
from api.billing.routes import (offtaker_vacancy, add_exchange_demand,
                                list_exchange_demand, _DemandBody)


def _excess_json(*, shared_kwh=0.0, credited_kwh=0.0, credited_usd=0.0):
    """A GMP-shaped raw_json: a $0 EXCESS line (shared out to members) and/or a
    negative-$ credited residual EXCESS line (retained + cashed by the host)."""
    items = []
    if shared_kwh:
        items.append({"unitOfMeasure": "KWH", "unitCode": "EXCESS",
                      "unitCount": shared_kwh, "dollarAmount": 0.0})
    if credited_kwh:
        items.append({"unitOfMeasure": "KWH", "unitCode": "EXCESS",
                      "unitCount": credited_kwh, "dollarAmount": -abs(credited_usd)})
    return {"billSegments": [{"segmentLineItems": items}]}


def _month_bills(db, tid, acct_id, *, months, pool, raw_maker, solar_credit_usd):
    """Seed `months` consecutive monthly host bills ending last month."""
    end = _now().replace(day=1) - timedelta(days=1)
    for i in range(months):
        pe = end - timedelta(days=30 * i)
        ps = pe - timedelta(days=29)
        db.add(Bill(tenant_id=tid, account_id=acct_id,
                    period_start=ps, period_end=pe,
                    kwh_generated=int(pool * 1.02),
                    kwh_sent_to_grid=pool,
                    solar_credit_usd=solar_credit_usd,
                    raw_json=raw_maker(),
                    is_net_metered=True))


def _seed_array(db, tid, name, *, pool, raw_maker, solar_credit_usd, shares):
    arr = Array(tenant_id=tid, name=name, region="VT",
                first_connect_date=datetime(2016, 5, 1))
    db.add(arr); db.flush()
    host = UtilityAccount(tenant_id=tid, provider="gmp",
                          account_number=f"HOST-{name}", array_id=arr.id,
                          nickname=f"{name} host")
    db.add(host); db.flush()
    _month_bills(db, tid, host.id, months=12, pool=pool,
                 raw_maker=raw_maker, solar_credit_usd=solar_credit_usd)
    for i, sh in enumerate(shares):
        c = Client(tenant_id=tid, name=f"{name} off {i}", active=True)
        db.add(c); db.flush()
        db.add(BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name=f"{name} off {i}",
            array_id=arr.id, allocation_pct=1.0, array_share_pct=sh,
            utility_account_id=None, billing_model="percent_of_array",
            cadence="monthly", enabled=True))
    db.flush()
    return arr.id


def _seed():
    tid = "ten_vac_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="VacCo",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        # Londonderry: perpetual banker — retained on host, NO $0 shared line,
        # banked (solar_credit_usd=None). One tiny 2% offtaker → registry agrees.
        lond = _seed_array(
            db, tid, "Londonderry",
            pool=56000.0,
            raw_maker=lambda: _excess_json(credited_kwh=9, credited_usd=1.66),
            solar_credit_usd=None,
            shares=[0.02])
        # Danville: fully allocated — the whole pool on the $0 shared line, tiny
        # residual. Offtaker shares sum ~1.0 → registry agrees (~0% vacant).
        danv = _seed_array(
            db, tid, "Danville",
            pool=30000.0,
            raw_maker=lambda: _excess_json(shared_kwh=29900, credited_kwh=100,
                                           credited_usd=18.4),
            solar_credit_usd=None,
            shares=[0.50, 0.497])
        db.commit()
        return tid, lond, danv


# ── the walker ────────────────────────────────────────────────────────────────

def test_split_excess_line_items():
    s = split_excess_line_items(_excess_json(shared_kwh=29900, credited_kwh=100,
                                             credited_usd=18.4))
    assert s["has_lines"] is True
    assert abs(s["shared_kwh"] - 29900) < 0.1
    assert abs(s["credited_kwh"] - 100) < 0.1
    # No line items → has_lines False (caller falls back to the pool total).
    assert split_excess_line_items({})["has_lines"] is False
    assert split_excess_line_items(None)["has_lines"] is False


# ── the two exemplars ─────────────────────────────────────────────────────────

def test_londonderry_is_fully_vacant():
    tid, lond, danv = _seed()
    with SessionLocal() as db:
        arr = db.get(Array, lond)
        v = array_vacancy(db, arr)
    # Perpetual banker: essentially the whole pool is retained/vacant.
    assert v["vacancy_frac"] > 0.98, v
    assert v["months_of_history"] == 12
    assert v["vacancy_kwh"] > 600000  # ~56k/mo × 12
    assert v["vacancy_usd"] > 0
    # Bill (~100%) vs registry (98% vacant) agree within tolerance → high.
    assert v["confidence"] == "high", v
    # Banked months roll toward the ~12-month cliff → something is nearing expiry.
    assert v["expiring_soon_kwh"] > 0, v


def test_danville_is_fully_allocated():
    tid, lond, danv = _seed()
    with SessionLocal() as db:
        arr = db.get(Array, danv)
        v = array_vacancy(db, arr)
    # Fully shared out to members → ~0% vacant.
    assert v["vacancy_frac"] < 0.02, v
    assert v["registry_vacancy_frac"] < 0.02, v
    assert v["confidence"] == "high", v


def test_registry_only_when_no_host_bill():
    """An array with offtaker shares but no host bill → registry estimate, medium
    confidence, honest note — never a fabricated bill-side number."""
    tid = "ten_reg_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="RegCo",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="NoBill", region="VT")
        db.add(arr); db.flush()
        # host account exists but NO bills
        db.add(UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number="H-nobill", array_id=arr.id))
        c = Client(tenant_id=tid, name="o", active=True); db.add(c); db.flush()
        db.add(BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="o",
            array_id=arr.id, allocation_pct=1.0, array_share_pct=0.80,
            cadence="monthly", enabled=True))
        db.commit()
        arr_row = db.get(Array, arr.id)
        v = array_vacancy(db, arr_row)
    assert v["confidence"] == "medium"
    assert abs(v["registry_vacancy_frac"] - 0.20) < 1e-6
    assert v["vacancy_frac"] == round(0.20, 4)


# ── the tenant rollup + endpoint ──────────────────────────────────────────────

def test_tenant_vacancy_and_endpoint():
    tid, lond, danv = _seed()
    with SessionLocal() as db:
        out = tenant_vacancy(db, tid)
    assert out["totals"]["array_count"] == 2
    # Most-vacant-dollars first → Londonderry on top.
    assert out["arrays"][0]["array_name"] == "Londonderry"
    assert out["totals"]["vacancy_usd"] > 0

    # Endpoint is tenant-scoped and returns the same shape.
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    res = offtaker_vacancy(authorization=auth)
    assert res["totals"]["array_count"] == 2
    assert len(res["arrays"]) == 2


# ── demand intake ─────────────────────────────────────────────────────────────

def test_demand_intake_roundtrip():
    tid, lond, danv = _seed()
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    r = add_exchange_demand(
        _DemandBody(contact_name="Jane Doe", contact_email="jane@x.com",
                    utility="GMP", desired_band="~2,000 kWh/mo",
                    monthly_bill_usd=180.0, notes="called Tuesday"),
        authorization=auth)
    assert r["ok"] and r["id"]
    listing = list_exchange_demand(authorization=auth)
    assert listing["count"] == 1
    lead = listing["leads"][0]
    assert lead["contact_name"] == "Jane Doe"
    assert lead["utility"] == "gmp"       # normalized lower
    assert lead["source"] == "operator_waitlist"
    assert lead["status"] == "new"


def test_demand_requires_name_or_email():
    tid, lond, danv = _seed()
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    try:
        add_exchange_demand(_DemandBody(utility="gmp"), authorization=auth)
        assert False, "should have raised 422"
    except Exception as e:
        assert getattr(e, "status_code", None) == 422


# ── synthetic-tenant guard (cross-tenant hygiene) ─────────────────────────────

def test_synthetic_tenant_guard():
    class _T:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    assert is_synthetic_tenant(_T(id="ten_demo_realistic", is_demo=False)) is True
    assert is_synthetic_tenant(_T(id="ten_ford_demo_100", is_demo=False)) is True
    assert is_synthetic_tenant(_T(id="ten_real", is_demo=True)) is True
    assert is_synthetic_tenant(_T(id="ten_real", is_demo=False, plan="demo")) is True
    assert is_synthetic_tenant(_T(id="ten_real", is_demo=False, plan="pro")) is False
    assert is_synthetic_tenant(None) is True
