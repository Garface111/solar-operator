"""
Tests for GET /v1/account/clients/{client_id}/production

Synthetic Bills spanning 18 months across two arrays with different
bill_offset_months values. Verifies:
  - monthly aggregation correctness
  - bill_offset_months is respected (GMP-style offset=1 vs same-month offset=0)
  - excluded arrays are excluded
  - YoY/TTM pcts are null when data is insufficient
  - 404 on wrong tenant
  - empty state when no data
"""
from __future__ import annotations

import secrets
from datetime import datetime

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Bill, Client, Tenant, UtilityAccount


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _advance_month(year: int, month: int, n: int) -> tuple[int, int]:
    m = month + n
    y = year + (m - 1) // 12
    m = ((m - 1) % 12) + 1
    return y, m


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Prod EP Test", contact_email=f"{tid}@test.com",
                      tenant_key=key, plan="standard", active=True))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _setup_full_scenario() -> tuple[str, str, int, int, int]:
    """Tenant with client owning 3 arrays (2 active, 1 excluded).

    Array 1 (offset=1, GMP-style): 18 bills, period_end months 2024-07→2026-01
      → production months 2024-06→2025-12, kwh = 100*(i+1)
    Array 2 (offset=0, same-month): 18 bills, period_end months 2024-07→2026-01
      → production months 2024-07→2026-01, kwh = 50*(i+1)
    Array 3 (offset=1, excluded): same bills as arr1 but should never appear.

    Returns (tid, auth, client_id, arr1_id, arr2_id).
    """
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    start_year, start_month = 2024, 7

    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Sunny Co", contact_email=f"{tid}@test.com",
                      tenant_key=key, plan="standard", active=True))
        db.flush()

        client = Client(tenant_id=tid, name="Valley Solar", contact_email="vs@test.com", active=True)
        db.add(client)
        db.flush()

        arr1 = Array(tenant_id=tid, client_id=client.id, name="North Ridge",
                     bill_offset_months=1, excluded=False)
        arr2 = Array(tenant_id=tid, client_id=client.id, name="South Field",
                     bill_offset_months=0, excluded=False)
        arr3 = Array(tenant_id=tid, client_id=client.id, name="Pittsfield",
                     bill_offset_months=1, excluded=True)
        db.add_all([arr1, arr2, arr3])
        db.flush()

        acct1 = UtilityAccount(tenant_id=tid, array_id=arr1.id, provider="gmp",
                               account_number=f"GMP1-{tid[:8]}")
        acct2 = UtilityAccount(tenant_id=tid, array_id=arr2.id, provider="gmp",
                               account_number=f"GMP2-{tid[:8]}")
        acct3 = UtilityAccount(tenant_id=tid, array_id=arr3.id, provider="gmp",
                               account_number=f"GMP3-{tid[:8]}")
        db.add_all([acct1, acct2, acct3])
        db.flush()

        for i in range(18):
            y, m = _advance_month(start_year, start_month, i)
            period_end = datetime(y, m, 28)
            period_start = datetime(y, m, 1)

            db.add(Bill(tenant_id=tid, account_id=acct1.id,
                        period_end=period_end, period_start=period_start,
                        kwh_generated=100 * (i + 1),
                        document_number=f"D1-{tid[:8]}-{i}"))
            db.add(Bill(tenant_id=tid, account_id=acct2.id,
                        period_end=period_end, period_start=period_start,
                        kwh_generated=50 * (i + 1),
                        document_number=f"D2-{tid[:8]}-{i}"))
            db.add(Bill(tenant_id=tid, account_id=acct3.id,
                        period_end=period_end, period_start=period_start,
                        kwh_generated=9999 * (i + 1),
                        document_number=f"D3-{tid[:8]}-{i}"))

        db.commit()
        return tid, f"Bearer {mint_session_for_tenant(tid)}", client.id, arr1.id, arr2.id


# ── (1) Basic response shape ───────────────────────────────────────────────────

def test_production_returns_ok_shape(client):
    tid, auth, cid, _, _ = _setup_full_scenario()
    resp = client.get(f"/v1/account/clients/{cid}/production?months=12",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "months" in body
    assert "stats" in body
    stats = body["stats"]
    assert "last_30_days" in stats
    assert "last_12_months" in stats
    assert "ytd" in stats
    assert "mwh" in stats["last_30_days"]
    assert "vs_prev_year_pct" in stats["last_30_days"]
    assert "mwh" in stats["last_12_months"]
    assert "vs_prev_ttm_pct" in stats["last_12_months"]
    assert "mwh" in stats["ytd"]


# ── (2) Correct number of months returned ─────────────────────────────────────

def test_production_months_count(client):
    tid, auth, cid, _, _ = _setup_full_scenario()
    resp = client.get(f"/v1/account/clients/{cid}/production?months=12",
                      headers={"Authorization": auth})
    body = resp.json()
    # With 18 bills per array and different offsets we get ≥12 unique months;
    # with months=12 the endpoint returns exactly 12.
    assert len(body["months"]) == 12


# ── (3) bill_offset_months is respected ───────────────────────────────────────

def test_production_offset_months_applied(client):
    """arr1 (offset=1) bill in period 2024-07 → production 2024-06.
       arr2 (offset=0) bill in period 2024-07 → production 2024-07.
       So 2024-06 has only arr1 data and 2024-07 has data from both."""
    tid, auth, cid, arr1_id, arr2_id = _setup_full_scenario()
    resp = client.get(f"/v1/account/clients/{cid}/production?months=36",
                      headers={"Authorization": auth})
    body = resp.json()
    months_by_key = {m["month"]: m for m in body["months"]}

    # 2024-06: only arr1 (kwh=100 → mwh=0.1)
    assert "2024-06" in months_by_key
    m_jun = months_by_key["2024-06"]
    assert abs(m_jun["mwh"] - 0.1) < 0.001
    assert len(m_jun["by_array"]) == 1
    assert m_jun["by_array"][0]["array_id"] == arr1_id

    # 2024-07: arr1 (bill i=1 → kwh=200 → 0.2) + arr2 (bill i=0 → kwh=50 → 0.05)
    assert "2024-07" in months_by_key
    m_jul = months_by_key["2024-07"]
    assert abs(m_jul["mwh"] - 0.25) < 0.001
    array_ids_in_jul = {x["array_id"] for x in m_jul["by_array"]}
    assert arr1_id in array_ids_in_jul
    assert arr2_id in array_ids_in_jul


# ── (4) Excluded arrays are excluded ─────────────────────────────────────────

def test_excluded_array_absent_from_production(client):
    """arr3 is excluded=True; its 9999-kwh bills must never appear."""
    tid, auth, cid, arr1_id, arr2_id = _setup_full_scenario()
    resp = client.get(f"/v1/account/clients/{cid}/production?months=36",
                      headers={"Authorization": auth})
    body = resp.json()
    for m in body["months"]:
        for arr in m["by_array"]:
            assert arr["array_id"] in (arr1_id, arr2_id), \
                f"Excluded array appeared in month {m['month']}: {arr}"
        # No month should have mwh anywhere near 9999*anything/1000
        assert m["mwh"] < 1000, f"Suspiciously large mwh in {m['month']}: {m['mwh']}"


# ── (5) YoY/TTM pct null when insufficient history ────────────────────────────

def test_yoy_null_without_prior_year(client):
    """A tenant with only 6 months of data has no prior-year comparison."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Short History Co", contact_email=f"{tid}@t.com",
                      tenant_key=key, plan="standard", active=True))
        db.flush()
        cli = Client(tenant_id=tid, name="Short", active=True)
        db.add(cli)
        db.flush()
        arr = Array(tenant_id=tid, client_id=cli.id, name="Meadow", bill_offset_months=1, excluded=False)
        db.add(arr)
        db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number=f"GS-{tid[:8]}")
        db.add(acct)
        db.flush()
        # Only 6 months of bills → 6 production months → no YoY data
        for i in range(6):
            y, m = _advance_month(2025, 7, i)
            db.add(Bill(tenant_id=tid, account_id=acct.id,
                        period_end=datetime(y, m, 28), period_start=datetime(y, m, 1),
                        kwh_generated=500, document_number=f"DS-{tid[:8]}-{i}"))
        db.commit()
        cid = cli.id

    auth = f"Bearer {mint_session_for_tenant(tid)}"
    resp = client.get(f"/v1/account/clients/{cid}/production?months=12",
                      headers={"Authorization": auth})
    body = resp.json()
    assert body["stats"]["last_30_days"]["vs_prev_year_pct"] is None
    assert body["stats"]["last_12_months"]["vs_prev_ttm_pct"] is None


# ── (6) Empty state (no accounts) returns correct structure ──────────────────

def test_production_empty_state(client):
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        cli = Client(tenant_id=tid, name="Empty Client", active=True)
        db.add(cli)
        db.commit()
        cid = cli.id

    resp = client.get(f"/v1/account/clients/{cid}/production",
                      headers={"Authorization": auth})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["months"] == []
    assert body["stats"]["last_30_days"]["mwh"] == 0.0
    assert body["stats"]["ytd"]["mwh"] == 0.0


# ── (7) Wrong tenant gets 404 ──────────────────────────────────────────────────

def test_production_wrong_tenant_404(client):
    tid, auth, cid, _, _ = _setup_full_scenario()
    other_tid, other_auth = _make_tenant()

    resp = client.get(f"/v1/account/clients/{cid}/production",
                      headers={"Authorization": other_auth})
    assert resp.status_code == 404


# ── (8) per_array breakdown present and sums to month total ──────────────────

def test_by_array_sums_to_month_total(client):
    tid, auth, cid, _, _ = _setup_full_scenario()
    resp = client.get(f"/v1/account/clients/{cid}/production?months=12",
                      headers={"Authorization": auth})
    body = resp.json()
    for m in body["months"]:
        arr_sum = sum(a["mwh"] for a in m["by_array"])
        assert abs(arr_sum - m["mwh"]) < 0.01, \
            f"by_array sum {arr_sum} != month total {m['mwh']} for {m['month']}"
