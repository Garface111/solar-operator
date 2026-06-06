"""
Tests for GET /v1/account/arrays/{array_id}/daily-coverage
"""
from __future__ import annotations

import secrets
from datetime import date

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, DailyGeneration, Tenant


def _make_tenant_with_array() -> tuple[str, str, int]:
    """Create Tenant → Client → Array. Returns (tenant_id, session_auth, array_id)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Coverage Test Co",
            contact_email=f"{tid}@test.com",
            tenant_key="k_" + secrets.token_hex(8),
            plan="standard",
            active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="Cov Client", active=True)
        db.add(c)
        db.flush()
        arr = Array(
            tenant_id=tid,
            client_id=c.id,
            name="Cov Array",
        )
        db.add(arr)
        db.flush()
        arr_id = arr.id
        db.commit()
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    return tid, auth, arr_id


def _seed_daily_gen(array_id: int, tenant_id: str, days: list[date], source: str = "csv") -> None:
    with SessionLocal() as db:
        for d in days:
            db.add(DailyGeneration(
                tenant_id=tenant_id,
                array_id=array_id,
                day=d,
                kwh=100.0,
                source=source,
            ))
        db.commit()


def test_coverage_zero_rows(client):
    """Array with no DailyGeneration rows returns day_count=0 and nulls."""
    _, auth, arr_id = _make_tenant_with_array()
    resp = client.get(
        f"/v1/account/arrays/{arr_id}/daily-coverage",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["day_count"] == 0
    assert data["first_day"] is None
    assert data["last_day"] is None
    assert data["source_counts"] == {}


def test_coverage_30_rows(client):
    """Array with 30 DailyGeneration rows returns correct count and date range."""
    tid, auth, arr_id = _make_tenant_with_array()
    days = [date(2024, 7, d) for d in range(1, 31)]
    _seed_daily_gen(arr_id, tid, days)

    resp = client.get(
        f"/v1/account/arrays/{arr_id}/daily-coverage",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["day_count"] == 30
    assert data["first_day"] == "2024-07-01"
    assert data["last_day"] == "2024-07-30"
    assert data["source_counts"] == {"csv": 30}


def test_coverage_100_rows(client):
    """Array with 100 DailyGeneration rows returns correct count."""
    tid, auth, arr_id = _make_tenant_with_array()
    days = []
    for m in (7, 8, 9, 10):  # Jul–Oct (31+31+30+8 = 100 days)
        import calendar
        _, last = calendar.monthrange(2024, m)
        for d in range(1, last + 1):
            if len(days) >= 100:
                break
            days.append(date(2024, m, d))
        if len(days) >= 100:
            break
    days = days[:100]
    _seed_daily_gen(arr_id, tid, days)

    resp = client.get(
        f"/v1/account/arrays/{arr_id}/daily-coverage",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["day_count"] == 100
    assert data["source_counts"]["csv"] == 100


def test_coverage_mixed_sources(client):
    """source_counts reflects multiple source types correctly."""
    tid, auth, arr_id = _make_tenant_with_array()
    csv_days = [date(2024, 7, d) for d in range(1, 11)]    # 10 csv rows
    manual_days = [date(2024, 8, d) for d in range(1, 6)]  # 5 manual rows
    _seed_daily_gen(arr_id, tid, csv_days, source="csv")
    _seed_daily_gen(arr_id, tid, manual_days, source="manual")

    resp = client.get(
        f"/v1/account/arrays/{arr_id}/daily-coverage",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["day_count"] == 15
    assert data["source_counts"]["csv"] == 10
    assert data["source_counts"]["manual"] == 5


def test_coverage_tenant_scoped(client):
    """Tenant B cannot access Tenant A's array coverage — returns 404."""
    _tid_a, _auth_a, arr_id_a = _make_tenant_with_array()
    _tid_b, auth_b, _ = _make_tenant_with_array()

    resp = client.get(
        f"/v1/account/arrays/{arr_id_a}/daily-coverage",
        headers={"Authorization": auth_b},
    )
    assert resp.status_code == 404
