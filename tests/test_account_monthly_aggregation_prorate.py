"""
Tests that client_production (account.py) uses pro-rate (not period_end-based
single-month attribution) for cross-month bills.

Cross-month bill: period_start=2025-04-11, period_end=2025-05-12, kwh=32000
  Total 32 days. April: 20 days, May: 12 days.
  Pro-rate kWh: April=20000, May=12000

With offset=1 (GMP-style):
  April pro-rate kWh (20000) → attributed to March  (April - 1)
  May   pro-rate kWh (12000) → attributed to April  (May - 1)
  → chart shows March=20.000 MWh, April=12.000 MWh

With offset=0 (same-month):
  April pro-rate kWh (20000) → April
  May   pro-rate kWh (12000) → May
  → chart shows April=20.000 MWh, May=12.000 MWh

OLD behavior (period_end month – offset, full kwh):
  offset=1: period_end=2025-05-12 → May - 1 = April → 32.000 MWh in April
  offset=0: period_end=2025-05-12 → May → 32.000 MWh in May
"""
from __future__ import annotations

import secrets
from datetime import datetime

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Bill, Client, Tenant, UtilityAccount


def _setup_cross_month_scenario(offset: int) -> tuple[str, str, int]:
    """One array with one cross-month bill. Returns (tid, auth, client_id)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Prorate Acct Test", contact_email=f"{tid}@test.com",
                      tenant_key=key, plan="standard", active=True))
        db.flush()

        c = Client(tenant_id=tid, name="River Solar", active=True)
        db.add(c); db.flush()

        arr = Array(tenant_id=tid, client_id=c.id, name="Creek Array",
                    bill_offset_months=offset, excluded=False)
        db.add(arr); db.flush()

        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number=f"CR-{tid[:8]}")
        db.add(acct); db.flush()

        # Cross-month bill: April 11 → May 12, 2025
        db.add(Bill(
            tenant_id=tid, account_id=acct.id,
            period_start=datetime(2025, 4, 11),
            period_end=datetime(2025, 5, 12),
            kwh_generated=32000,
            document_number=f"cross-{tid}",
        ))
        db.commit()

    auth = f"Bearer {mint_session_for_tenant(tid)}"
    return tid, auth, c.id


def test_offset0_cross_month_splits_into_april_and_may(client):
    """offset=0: bill April 11–May 12 → April=20 MWh, May=12 MWh (pro-rate).
    Old code put everything in May (period_end month, no offset)."""
    _, auth, cid = _setup_cross_month_scenario(offset=0)
    resp = client.get(f"/v1/account/clients/{cid}/production?months=36",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    months_by_key = {m["month"]: m for m in body["months"]}

    assert "2025-04" in months_by_key, f"April missing from months: {list(months_by_key)}"
    assert "2025-05" in months_by_key, f"May missing from months: {list(months_by_key)}"

    apr_mwh = months_by_key["2025-04"]["mwh"]
    may_mwh = months_by_key["2025-05"]["mwh"]

    assert abs(apr_mwh - 20.0) < 0.01, (
        f"April={apr_mwh} MWh, expected 20.000. Old code put everything in May."
    )
    assert abs(may_mwh - 12.0) < 0.01, (
        f"May={may_mwh} MWh, expected 12.000."
    )


def test_offset1_cross_month_shifts_buckets_by_one_month(client):
    """offset=1: April days → March, May days → April.
    Old code: full bill (period_end May 12, offset 1) → April = 32 MWh (wrong)."""
    _, auth, cid = _setup_cross_month_scenario(offset=1)
    resp = client.get(f"/v1/account/clients/{cid}/production?months=36",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    months_by_key = {m["month"]: m for m in body["months"]}

    assert "2025-03" in months_by_key, f"March missing: {list(months_by_key)}"
    assert "2025-04" in months_by_key, f"April missing: {list(months_by_key)}"

    mar_mwh = months_by_key["2025-03"]["mwh"]
    apr_mwh = months_by_key["2025-04"]["mwh"]

    assert abs(mar_mwh - 20.0) < 0.01, (
        f"March={mar_mwh} MWh, expected 20.000 (April days shifted by offset=1)."
    )
    assert abs(apr_mwh - 12.0) < 0.01, (
        f"April={apr_mwh} MWh, expected 12.000 (May days shifted by offset=1)."
    )


def test_kwh_conservation_across_pro_rate_and_offset(client):
    """Sum of all monthly MWh must equal total bill kWh / 1000 = 32.0."""
    _, auth, cid = _setup_cross_month_scenario(offset=0)
    resp = client.get(f"/v1/account/clients/{cid}/production?months=36",
                      headers={"Authorization": auth})
    body = resp.json()
    total_mwh = sum(m["mwh"] for m in body["months"])
    assert abs(total_mwh - 32.0) < 0.01, (
        f"Total MWh across all months = {total_mwh}, expected 32.000"
    )
