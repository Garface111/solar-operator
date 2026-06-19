"""GMP capture→array link path.

Covers the manual link endpoint (the multi-meter bridge) and proves the full
chain: link a GMP account that carries bills → run bill→daily → the EXISTING
array now has daily production (it 'lights up'), no duplicate array created.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_link_test")

from datetime import datetime, date
import secrets

from sqlalchemy import select, func
from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, DailyGeneration, Client)
from api.array_owners import link_utility_account_ep, LinkAccountBody, list_utility_accounts_ep
from api.account import mint_session_for_tenant
from api.jobs.bill_to_daily import transform_tenant_bills


def _seed():
    tid = "ten_lnk_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Lnk",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        cl = Client(tenant_id=tid, name="Bruce", active=True); db.add(cl); db.flush()
        # Existing array, NO utility account linked (the AO situation).
        arr = Array(tenant_id=tid, client_id=cl.id, name="Starlake", region="VT")
        db.add(arr); db.flush()
        # A captured GMP account WITH bills, but array_id NULL (unlinked).
        acc = UtilityAccount(tenant_id=tid, provider="gmp", account_number="55501",
                             nickname="Starlake Meter A", array_id=None)
        db.add(acc); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acc.id,
                    period_start=datetime(2026, 4, 1), period_end=datetime(2026, 4, 30),
                    kwh_generated=3000, total_cost=600.0))
        db.commit()
        return tid, arr.id, acc.id


def test_manual_link_lights_up_existing_array():
    tid, aid, acc_id = _seed()
    tok = mint_session_for_tenant(tid)
    auth = f"Bearer {tok}"

    # Before: account is unlinked; list shows it with its bill_count.
    listing = list_utility_accounts_ep(authorization=auth)
    rec = [a for a in listing["accounts"] if a["account_id"] == acc_id][0]
    assert rec["linked_array_id"] is None and rec["bill_count"] == 1

    # Link the account → existing Starlake array.
    res = link_utility_account_ep(LinkAccountBody(account_id=acc_id, array_id=aid), authorization=auth)
    assert res["ok"] and res["linked_array_id"] == aid

    # Now the bill→daily transform should populate Starlake from the linked bill.
    r = transform_tenant_bills(tid)
    assert r["bills_seen"] == 1 and r["days_written"] == 30, r  # April = 30 days
    with SessionLocal() as db:
        n_arrays = db.execute(select(func.count(Array.id)).where(
            Array.tenant_id == tid, Array.deleted_at.is_(None))).scalar()
        assert n_arrays == 1   # NO duplicate array created
        days = db.execute(select(func.count(DailyGeneration.id)).where(
            DailyGeneration.array_id == aid,
            DailyGeneration.source == "bill_prorate")).scalar()
        assert days == 30
        total = db.execute(select(func.sum(DailyGeneration.kwh)).where(
            DailyGeneration.array_id == aid)).scalar()
        assert abs(float(total) - 3000.0) < 0.5   # 100 kWh/day × 30 = 3000


def test_unlink():
    tid, aid, acc_id = _seed()
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    link_utility_account_ep(LinkAccountBody(account_id=acc_id, array_id=aid), authorization=auth)
    res = link_utility_account_ep(LinkAccountBody(account_id=acc_id, array_id=None), authorization=auth)
    assert res["linked_array_id"] is None
