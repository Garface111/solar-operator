"""
Tests for POST /v1/account/arrays/{array_id}/daily-csv

Coverage:
- Format 1 (Daily Usage Export): YYYY-MM-DD + kWh Generated column
- Format 2 (Energy Dashboard): MM/DD/YYYY + Generation (kWh) column
- Format 3 fallback: no header, col 0 is date, col 1 is number
- Re-upload: same dates → updates, not duplicate inserts
- Tenant scoping: cannot upload to another tenant's array → 404
- Garbage CSV → 400 with first 3 rows in message
- Empty CSV → 400 "no data rows found"
- Rows with kwh < 0 or non-numeric → skipped, counted in rows_skipped
"""
from __future__ import annotations

import secrets
from datetime import date, datetime

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, DailyGeneration, Tenant, UtilityAccount


# ── fixture helpers ────────────────────────────────────────────────────────────


def _make_tenant_with_array() -> tuple[str, str, int]:
    """Create Tenant → Client → Array. Returns (tenant_id, session_auth, array_id)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Daily Gen Test Co",
            contact_email=f"{tid}@test.com",
            tenant_key="k_" + secrets.token_hex(8),
            plan="standard",
            active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="Test Client", active=True)
        db.add(c)
        db.flush()
        arr = Array(
            tenant_id=tid,
            client_id=c.id,
            name="Test Array",
            nepool_gis_id="12345",
        )
        db.add(arr)
        db.flush()
        arr_id = arr.id
        db.commit()
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    return tid, auth, arr_id


def _csv_bytes(content: str) -> bytes:
    return content.encode("utf-8")


# ── Format 1: Daily Usage Export ───────────────────────────────────────────────


def test_format1_happy_path(client):
    """30 rows of Format 1 parsed correctly, count and date range correct."""
    _, auth, arr_id = _make_tenant_with_array()

    lines = ["Date,kWh Generated,kWh Delivered,kWh Received"]
    for day in range(1, 31):
        lines.append(f"2024-07-{day:02d},{100 + day:.1f},0,0")
    csv_content = "\n".join(lines)

    resp = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("gen.csv", _csv_bytes(csv_content), "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rows_inserted"] == 30
    assert data["rows_updated"] == 0
    assert data["rows_skipped"] == 0
    assert data["date_range"]["start"] == "2024-07-01"
    assert data["date_range"]["end"] == "2024-07-30"
    assert data["source"] == "csv"


# ── Format 2: Energy Dashboard Export ──────────────────────────────────────────


def test_format2_quoted_mm_dd_yyyy(client):
    """Format 2 with quoted MM/DD/YYYY dates parses correctly."""
    _, auth, arr_id = _make_tenant_with_array()

    csv_content = (
        '"Service Date","Generation (kWh)","Net (kWh)"\n'
        '"7/1/2024","142.4","-142.4"\n'
        '"7/2/2024","158.7","-158.7"\n'
        '"7/3/2024","133.0","-133.0"\n'
    )
    resp = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("dashboard.csv", _csv_bytes(csv_content), "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rows_inserted"] == 3
    assert data["date_range"]["start"] == "2024-07-01"
    assert data["date_range"]["end"] == "2024-07-03"


# ── Format 3: no-header fallback ───────────────────────────────────────────────


def test_format3_no_header_fallback(client):
    """No-header CSV with (date, kwh) columns is accepted."""
    _, auth, arr_id = _make_tenant_with_array()

    csv_content = (
        "2024-08-01,200.0\n"
        "2024-08-02,210.5\n"
        "2024-08-03,195.2\n"
    )
    resp = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("raw.csv", _csv_bytes(csv_content), "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rows_inserted"] == 3
    assert data["date_range"]["start"] == "2024-08-01"
    assert data["date_range"]["end"] == "2024-08-03"


# ── Re-upload: same dates → updates ───────────────────────────────────────────


def test_reupload_same_dates_updates(client):
    """Re-uploading the same date range overwrites existing rows."""
    _, auth, arr_id = _make_tenant_with_array()

    csv_v1 = "Date,kWh Generated\n2024-09-01,100.0\n2024-09-02,200.0\n"
    csv_v2 = "Date,kWh Generated\n2024-09-01,150.0\n2024-09-02,250.0\n"

    r1 = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("v1.csv", _csv_bytes(csv_v1), "text/csv")},
        headers={"Authorization": auth},
    )
    assert r1.status_code == 200
    assert r1.json()["rows_inserted"] == 2
    assert r1.json()["rows_updated"] == 0

    r2 = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("v2.csv", _csv_bytes(csv_v2), "text/csv")},
        headers={"Authorization": auth},
    )
    assert r2.status_code == 200
    assert r2.json()["rows_inserted"] == 0
    assert r2.json()["rows_updated"] == 2

    # Verify the values were actually updated
    with SessionLocal() as db:
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()
        by_day = {r.day: r.kwh for r in rows}
    assert by_day[date(2024, 9, 1)] == 150.0
    assert by_day[date(2024, 9, 2)] == 250.0


# ── Tenant scoping ─────────────────────────────────────────────────────────────


def test_cannot_upload_to_other_tenants_array(client):
    """Tenant A cannot upload to Tenant B's array — returns 404."""
    _, _auth_a, arr_id_b = _make_tenant_with_array()  # array belongs to tenant A
    _, auth_b, _ = _make_tenant_with_array()            # tenant B's session

    csv_content = "Date,kWh Generated\n2024-07-01,100.0\n"
    resp = client.post(
        f"/v1/account/arrays/{arr_id_b}/daily-csv",
        files={"file": ("x.csv", _csv_bytes(csv_content), "text/csv")},
        headers={"Authorization": auth_b},
    )
    assert resp.status_code == 404


# ── Garbage CSV → 400 ─────────────────────────────────────────────────────────


def test_garbage_csv_returns_400_with_first_rows(client):
    """Completely unparseable CSV returns 400 and includes first rows in message."""
    _, auth, arr_id = _make_tenant_with_array()

    csv_content = "not,a,date,column\nblah,blah,blah,blah\nmore,garbage,here,x\n"
    resp = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("garbage.csv", _csv_bytes(csv_content), "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 400
    # Error message should include the first rows so the operator knows what we saw
    detail = resp.json()["detail"]
    assert "not" in detail or "blah" in detail or "garbage" in detail.lower()


# ── Empty CSV → 400 ────────────────────────────────────────────────────────────


def test_empty_csv_returns_400(client):
    """Uploading an empty file returns 400."""
    _, auth, arr_id = _make_tenant_with_array()

    resp = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("empty.csv", b"", "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 400


# ── Negative / non-numeric kWh → skipped ──────────────────────────────────────


def test_negative_and_nonnumeric_kwh_skipped(client):
    """Rows with negative kWh or non-numeric values are skipped."""
    _, auth, arr_id = _make_tenant_with_array()

    csv_content = (
        "Date,kWh Generated\n"
        "2024-10-01,100.0\n"         # valid
        "2024-10-02,-50.0\n"         # negative → skip
        "2024-10-03,not_a_number\n"  # non-numeric → skip
        "2024-10-04,200.0\n"         # valid
    )
    resp = client.post(
        f"/v1/account/arrays/{arr_id}/daily-csv",
        files={"file": ("mixed.csv", _csv_bytes(csv_content), "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rows_inserted"] == 2  # only Oct 1 and Oct 4
    assert data["rows_skipped"] == 2
