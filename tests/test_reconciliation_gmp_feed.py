"""Reconciliation: GMP daily sponge feeds the production leg, and a variance
backed only by utility-sourced production is flagged leak_unconfirmed (never an
asserted leak). Guards the audit's data-integrity contract."""
from __future__ import annotations

import secrets
from datetime import date, datetime

import pytest

from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, DailyGeneration,
                        GmpDailyGeneration)
from api.reconciliation import reconcile_array

_SEEDED: list[str] = []


def _seed(*, with_independent: bool, prod_kwh: float, settle_kwh: float):
    """One single-site array + one GMP account + one parsed bill spanning a full
    month, with daily production either from an INDEPENDENT source (solaredge) or
    only from the GMP sponge (utility meter)."""
    tid = "ten_recon_" + secrets.token_hex(4)
    _SEEDED.append(tid)
    ws, we = date(2025, 6, 1), date(2025, 6, 30)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Recon Test",
                      contact_email=f"{tid}@e.com"))
        db.flush()
        arr = Array(tenant_id=tid, name="Recon Array")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="GMP-" + secrets.token_hex(3))
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id, parse_status="parsed",
                    period_start=datetime(2025, 6, 1), period_end=datetime(2025, 6, 30),
                    billing_days=30, kwh_generated=settle_kwh))
        # 30 days of production
        per_day = prod_kwh / 30.0
        if with_independent:
            for i in range(30):
                db.add(DailyGeneration(tenant_id=tid, array_id=arr.id,
                                       day=date(2025, 6, 1 + i),
                                       kwh=per_day, source="solaredge"))
        else:
            for i in range(30):
                db.add(GmpDailyGeneration(tenant_id=tid, account_id=acct.id,
                                          account_number=acct.account_number,
                                          array_id=arr.id,
                                          day=date(2025, 6, 1 + i),
                                          kwh=per_day, interval_count=96, source="gmp_api"))
        db.commit()
        return arr.id, ws, we


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SessionLocal() as db:
        for tid in _SEEDED:
            arr_ids = [a.id for a in db.query(Array).filter(Array.tenant_id == tid)]
            acct_ids = [a.id for a in db.query(UtilityAccount).filter(UtilityAccount.tenant_id == tid)]
            if acct_ids:
                db.query(Bill).filter(Bill.account_id.in_(acct_ids)).delete(synchronize_session=False)
                db.query(GmpDailyGeneration).filter(GmpDailyGeneration.account_id.in_(acct_ids)).delete(synchronize_session=False)
            if arr_ids:
                db.query(DailyGeneration).filter(DailyGeneration.array_id.in_(arr_ids)).delete(synchronize_session=False)
            db.query(UtilityAccount).filter(UtilityAccount.tenant_id == tid).delete(synchronize_session=False)
            db.query(Array).filter(Array.tenant_id == tid).delete(synchronize_session=False)
            db.query(Tenant).filter(Tenant.id == tid).delete(synchronize_session=False)
        db.commit()
    _SEEDED.clear()


def test_gmp_sponge_fills_production_leg():
    """With NO DailyGeneration rows, the GMP daily sponge supplies the production
    leg so the array becomes auditable (matched production → 'ok')."""
    aid, ws, we = _seed(with_independent=False, prod_kwh=1000.0, settle_kwh=1000.0)
    with SessionLocal() as db:
        r = reconcile_array(db, aid, ws, we)
    assert r.production_kwh > 0, "GMP sponge should supply production"
    assert r.status == "ok", f"matched production should be ok, got {r.status}"


def test_utility_only_variance_is_unconfirmed_not_leak():
    """A big variance backed ONLY by GMP meter data must be leak_unconfirmed —
    we never assert a leak when reconciling the utility against itself."""
    aid, ws, we = _seed(with_independent=False, prod_kwh=1300.0, settle_kwh=1000.0)
    with SessionLocal() as db:
        r = reconcile_array(db, aid, ws, we)
    assert r.status == "leak_unconfirmed", f"got {r.status}"
    assert r.report_leak is False
    assert r.gates.get("independent_feed") is False


def test_independent_feed_variance_is_real_leak():
    """The same variance backed by an INDEPENDENT feed (solaredge) is a real,
    asserted leak with dollars at risk."""
    aid, ws, we = _seed(with_independent=True, prod_kwh=1300.0, settle_kwh=1000.0)
    with SessionLocal() as db:
        r = reconcile_array(db, aid, ws, we)
    assert r.status == "leak", f"got {r.status}"
    assert r.report_leak is True
    assert r.dollars_at_risk > 0
    assert r.gates.get("independent_feed") is True
