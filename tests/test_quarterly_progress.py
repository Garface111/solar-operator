"""Tests for api/quarterly.py: compute_quarterly_progress.

Uses a live SQLite test DB (via conftest.py) and the real ORM models.
All tenants, clients, arrays, accounts, and bills are created per-test
using a fresh session so tests are fully isolated.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime

import pytest

from api.db import SessionLocal
from api.models import Array, Bill, Client, Tenant, UtilityAccount
from api.quarterly import (
    _bill_covers_month,
    _quarter_end,
    _quarter_months,
    _quarter_of,
    _quarter_start,
    compute_quarterly_progress,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _new_tenant() -> str:
    tid = "ten_qp_" + secrets.token_hex(4)
    with SessionLocal() as db:
        db.add(
            Tenant(
                id=tid,
                name="QP Test",
                contact_email=f"{tid}@test.test",
                tenant_key="sol_live_" + secrets.token_urlsafe(8),
                plan="standard",
                active=True,
            )
        )
        db.commit()
    return tid


def _new_client(tid: str, name: str = "Test Client") -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name=name, active=True)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def _new_array(tid: str, cid: int, name: str = "Test Array", excluded: bool = False) -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, client_id=cid, name=name, excluded=excluded)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id


def _new_account(tid: str, arr_id: int | None, number: str = "ACC001") -> int:
    with SessionLocal() as db:
        a = UtilityAccount(
            tenant_id=tid,
            array_id=arr_id,
            provider="gmp",
            account_number=number,
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id


def _new_bill(
    tid: str,
    acct_id: int,
    period_start: date,
    period_end: date,
    kwh: int = 1000,
) -> int:
    with SessionLocal() as db:
        b = Bill(
            tenant_id=tid,
            account_id=acct_id,
            period_start=datetime.combine(period_start, datetime.min.time()),
            period_end=datetime.combine(period_end, datetime.min.time()),
            bill_date=datetime.combine(period_end, datetime.min.time()),
            kwh_generated=kwh,
            document_number=secrets.token_hex(6),
        )
        db.add(b)
        db.commit()
        db.refresh(b)
        return b.id


# ── Helper unit tests ─────────────────────────────────────────────────────────


def test_quarter_of():
    assert _quarter_of(1) == 1
    assert _quarter_of(3) == 1
    assert _quarter_of(4) == 2
    assert _quarter_of(6) == 2
    assert _quarter_of(7) == 3
    assert _quarter_of(9) == 3
    assert _quarter_of(10) == 4
    assert _quarter_of(12) == 4


def test_quarter_months():
    assert _quarter_months(2026, 2) == [(2026, 4), (2026, 5), (2026, 6)]
    assert _quarter_months(2026, 4) == [(2026, 10), (2026, 11), (2026, 12)]
    assert _quarter_months(2025, 1) == [(2025, 1), (2025, 2), (2025, 3)]


def test_quarter_start_end():
    assert _quarter_start(2026, 2) == date(2026, 4, 1)
    assert _quarter_end(2026, 2) == date(2026, 6, 30)
    assert _quarter_end(2026, 4) == date(2026, 12, 31)


def test_bill_covers_month_basic():
    """Bill whose period spans a month returns True for that month."""

    class FakeBill:
        period_start = datetime(2026, 4, 1)
        period_end = datetime(2026, 4, 30)
        bill_date = datetime(2026, 5, 1)
        kwh_generated = 500

    assert _bill_covers_month(FakeBill(), 2026, 4) is True
    assert _bill_covers_month(FakeBill(), 2026, 5) is False
    assert _bill_covers_month(FakeBill(), 2026, 3) is False


def test_bill_covers_month_cross_month():
    """Bill spanning two months covers both."""

    class FakeBill:
        period_start = datetime(2026, 3, 15)
        period_end = datetime(2026, 4, 14)
        bill_date = datetime(2026, 4, 15)
        kwh_generated = 500

    assert _bill_covers_month(FakeBill(), 2026, 3) is True
    assert _bill_covers_month(FakeBill(), 2026, 4) is True
    assert _bill_covers_month(FakeBill(), 2026, 5) is False


def test_bill_covers_month_fallback_to_bill_date():
    """When period_start and period_end are both None, fall back to bill_date month."""

    class FakeBill:
        period_start = None
        period_end = None
        bill_date = datetime(2026, 6, 15)
        kwh_generated = 800

    assert _bill_covers_month(FakeBill(), 2026, 6) is True
    assert _bill_covers_month(FakeBill(), 2026, 5) is False


# ── compute_quarterly_progress integration tests ──────────────────────────────


def test_all_arrays_ready():
    """All three quarter months have bills — all_ready is True."""
    tid = _new_tenant()
    cid = _new_client(tid)
    aid = _new_array(tid, cid)
    acct_id = _new_account(tid, aid)

    # Q2-2026: April, May, June
    today = date(2026, 6, 1)
    _new_bill(tid, acct_id, date(2026, 4, 1), date(2026, 4, 30))
    _new_bill(tid, acct_id, date(2026, 5, 1), date(2026, 5, 31))
    _new_bill(tid, acct_id, date(2026, 6, 1), date(2026, 6, 30))

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=today)

    assert result["all_ready"] is True
    assert result["total_arrays"] == 1
    assert len(result["ready_arrays"]) == 1
    assert len(result["missing_arrays"]) == 0
    assert result["quarter"] == "Q2-2026"


def test_no_arrays_ready():
    """No bills at all — all months missing, all_ready False."""
    tid = _new_tenant()
    cid = _new_client(tid)
    aid = _new_array(tid, cid, name="Empty Array")
    _new_account(tid, aid)

    today = date(2026, 6, 15)

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=today)

    assert result["all_ready"] is False
    assert len(result["ready_arrays"]) == 0
    assert len(result["missing_arrays"]) == 1
    assert set(result["missing_arrays"][0]["missing_months"]) == {
        "2026-04", "2026-05", "2026-06"
    }


def test_mixed_ready_and_missing():
    """Two arrays: one fully covered, one missing a month."""
    tid = _new_tenant()
    cid = _new_client(tid)

    # Array 1 — all months covered
    aid1 = _new_array(tid, cid, name="Full Array")
    acct1 = _new_account(tid, aid1, "ACC001")
    today = date(2026, 9, 15)  # Q3-2026: Jul, Aug, Sep
    _new_bill(tid, acct1, date(2026, 7, 1), date(2026, 7, 31))
    _new_bill(tid, acct1, date(2026, 8, 1), date(2026, 8, 31))
    _new_bill(tid, acct1, date(2026, 9, 1), date(2026, 9, 30))

    # Array 2 — missing September
    aid2 = _new_array(tid, cid, name="Missing Sep")
    acct2 = _new_account(tid, aid2, "ACC002")
    _new_bill(tid, acct2, date(2026, 7, 1), date(2026, 7, 31))
    _new_bill(tid, acct2, date(2026, 8, 1), date(2026, 8, 31))

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=today)

    assert result["all_ready"] is False
    assert result["total_arrays"] == 2
    assert len(result["ready_arrays"]) == 1
    assert result["ready_arrays"][0]["name"] == "Full Array"
    assert len(result["missing_arrays"]) == 1
    assert result["missing_arrays"][0]["name"] == "Missing Sep"
    assert result["missing_arrays"][0]["missing_months"] == ["2026-09"]


def test_excluded_arrays_omitted():
    """Excluded arrays do not appear in the readiness count."""
    tid = _new_tenant()
    cid = _new_client(tid)

    # Normal array with no bills
    aid1 = _new_array(tid, cid, name="Active")
    acct1 = _new_account(tid, aid1, "ACC001")
    today = date(2026, 6, 1)
    _new_bill(tid, acct1, date(2026, 4, 1), date(2026, 4, 30))
    _new_bill(tid, acct1, date(2026, 5, 1), date(2026, 5, 31))
    _new_bill(tid, acct1, date(2026, 6, 1), date(2026, 6, 30))

    # Excluded array (e.g. Pittsfield — below REC threshold)
    _new_array(tid, cid, name="Excluded", excluded=True)

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=today)

    assert result["total_arrays"] == 1
    assert result["all_ready"] is True


def test_empty_client_no_arrays():
    """Client with zero arrays: total_arrays=0, all_ready=False (vacuously false)."""
    tid = _new_tenant()
    cid = _new_client(tid)

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=date(2026, 6, 1))

    assert result["total_arrays"] == 0
    assert result["all_ready"] is False
    assert result["ready_arrays"] == []
    assert result["missing_arrays"] == []


def test_array_with_no_accounts():
    """Array with no linked accounts shows all months as missing."""
    tid = _new_tenant()
    cid = _new_client(tid)
    _new_array(tid, cid, name="No Accounts")

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=date(2026, 6, 15))

    assert len(result["missing_arrays"]) == 1
    assert len(result["missing_arrays"][0]["missing_months"]) == 3


def test_quarter_label_and_dates():
    """Response quarter label and date strings are correctly formatted."""
    tid = _new_tenant()
    cid = _new_client(tid)

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=date(2026, 4, 1))

    assert result["quarter"] == "Q2-2026"
    assert result["quarter_start"] == "2026-04-01"
    assert result["quarter_end"] == "2026-06-30"


def test_q4_boundary():
    """Q4 end date is December 31 (not year overflow)."""
    tid = _new_tenant()
    cid = _new_client(tid)

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=date(2026, 12, 15))

    assert result["quarter"] == "Q4-2026"
    assert result["quarter_end"] == "2026-12-31"


def test_bills_with_zero_kwh_ignored():
    """Bills with kwh_generated=0 do not count as coverage."""
    tid = _new_tenant()
    cid = _new_client(tid)
    aid = _new_array(tid, cid, name="Zero kWh")
    acct = _new_account(tid, aid)

    today = date(2026, 6, 1)
    # Zero-kwh bills — should not count
    _new_bill(tid, acct, date(2026, 4, 1), date(2026, 4, 30), kwh=0)

    with SessionLocal() as db:
        result = compute_quarterly_progress(cid, db, today=today)

    assert result["all_ready"] is False
    assert "2026-04" in result["missing_arrays"][0]["missing_months"]


def test_api_endpoint_returns_progress(client):
    """GET /v1/account/clients/{id}/quarterly_progress returns 200 with shape."""
    from api.account import mint_session_for_tenant

    tid = _new_tenant()
    cid = _new_client(tid, "API Test Client")
    auth = f"Bearer {mint_session_for_tenant(tid)}"

    resp = client.get(
        f"/v1/account/clients/{cid}/quarterly_progress",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "quarter" in data
    assert "total_arrays" in data
    assert "all_ready" in data
    assert "ready_arrays" in data
    assert "missing_arrays" in data


def test_api_endpoint_rejects_wrong_tenant(client):
    """Client belonging to another tenant returns 404."""
    from api.account import mint_session_for_tenant

    tid_owner = _new_tenant()
    tid_other = _new_tenant()
    cid = _new_client(tid_owner)
    auth = f"Bearer {mint_session_for_tenant(tid_other)}"

    resp = client.get(
        f"/v1/account/clients/{cid}/quarterly_progress",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 404
