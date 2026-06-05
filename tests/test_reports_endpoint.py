"""
Tests for the reports history endpoints:
  GET  /v1/account/reports?quarters=N
  GET  /v1/account/clients/{id}/report.xlsx?quarter=Q1-2026
  POST /v1/account/regenerate

Uses synthetic non-Bruce data. All tenants are isolated by unique IDs.

Today (conftest) = 2026-06-04 → current quarter Q2-2026.
Last complete quarter = Q1-2026 (Jan–Mar 2026).
"""
from __future__ import annotations

import secrets
from datetime import datetime, date

import pytest
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Bill, Client, Tenant, UtilityAccount


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_tenant() -> tuple[str, str]:
    """Create a minimal tenant; return (tenant_id, bearer_header)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Reports Test Co",
            contact_email=f"{tid}@reports.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard", active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _add_client(tenant_id: str, last_delivery_at: datetime | None = None) -> int:
    with SessionLocal() as db:
        c = Client(
            tenant_id=tenant_id, name="Test Client " + secrets.token_hex(3),
            active=True, last_delivery_at=last_delivery_at,
        )
        db.add(c); db.commit(); db.refresh(c)
        return c.id


def _add_array(tenant_id: str, client_id: int, name: str | None = None) -> int:
    with SessionLocal() as db:
        a = Array(
            tenant_id=tenant_id, client_id=client_id,
            name=name or ("Array " + secrets.token_hex(3)),
        )
        db.add(a); db.commit(); db.refresh(a)
        return a.id


def _add_account(tenant_id: str, array_id: int) -> int:
    with SessionLocal() as db:
        u = UtilityAccount(
            tenant_id=tenant_id, array_id=array_id,
            provider="gmp", account_number=secrets.token_hex(5),
        )
        db.add(u); db.commit(); db.refresh(u)
        return u.id


def _add_bill(account_id: int, tenant_id: str, period_start: date, kwh: int) -> None:
    with SessionLocal() as db:
        db.add(Bill(
            tenant_id=tenant_id, account_id=account_id,
            period_start=datetime.combine(period_start, datetime.min.time()),
            kwh_generated=kwh, kwh_consumed=0,
            document_number=secrets.token_hex(6),
        ))
        db.commit()


def _get_reports(client, auth: str, quarters: int = 6):
    return client.get(
        f"/v1/account/reports?quarters={quarters}",
        headers={"Authorization": auth},
    )


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestReportsEmpty:
    def test_no_arrays_all_empty(self, client):
        """Tenant with no arrays → all 6 quarters are 'empty'."""
        _, auth = _make_tenant()
        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        assert len(reports) == 6
        assert all(r["status"] == "empty" for r in reports)
        assert all(r["mwh_total"] == 0 for r in reports)
        assert all(r["array_count"] == 0 for r in reports)

    def test_no_bills_all_draft(self, client):
        """Tenant with arrays but no bill data → all past quarters are 'draft'."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        _add_array(tid, cid)
        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        assert all(r["status"] == "draft" for r in reports)

    def test_quarters_param_limits_count(self, client):
        """?quarters=3 returns exactly 3 quarters."""
        _, auth = _make_tenant()
        resp = _get_reports(client, auth, quarters=3)
        assert resp.status_code == 200
        assert len(resp.json()["reports"]) == 3

    def test_most_recent_quarter_is_first(self, client):
        """reports[0] should be the current in-progress quarter (Q2-2026)."""
        _, auth = _make_tenant()
        resp = _get_reports(client, auth, quarters=6)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        first = reports[0]
        # Today is 2026-06-04 → Q2-2026
        assert first["year"] == 2026
        assert first["quarter_num"] == 2
        assert first["quarter"] == "Q2-2026"


class TestReportsReady:
    def test_bill_in_past_quarter_gives_ready(self, client):
        """Bills in Q1-2026 (complete, no delivery recorded) → 'ready'."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)          # no last_delivery_at
        aid = _add_array(tid, cid)
        uid = _add_account(tid, aid)
        # Q1-2026: January, February, March
        for m in (1, 2, 3):
            _add_bill(uid, tid, date(2026, m, 15), kwh=10_000)

        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        q1 = next(r for r in reports if r["quarter"] == "Q1-2026")
        assert q1["status"] == "ready"
        assert q1["array_count"] == 1
        assert q1["mwh_total"] == pytest.approx(30.0, abs=0.01)

    def test_mwh_total_aggregates_correctly(self, client):
        """Two arrays each with 5000 kWh in Q4-2025 → 10 MWh total."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        for _ in range(2):
            aid = _add_array(tid, cid)
            uid = _add_account(tid, aid)
            _add_bill(uid, tid, date(2025, 10, 1), kwh=5_000)

        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        q4 = next(r for r in reports if r["quarter"] == "Q4-2025")
        assert q4["status"] == "ready"
        assert q4["mwh_total"] == pytest.approx(10.0, abs=0.01)
        assert q4["array_count"] == 2

    def test_zero_kwh_bills_ignored(self, client):
        """Bills with kwh_generated=0 or None don't count toward mwh_total."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        aid = _add_array(tid, cid)
        uid = _add_account(tid, aid)
        _add_bill(uid, tid, date(2025, 7, 1), kwh=0)

        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        q3 = next(r for r in reports if r["quarter"] == "Q3-2025")
        assert q3["status"] == "draft"   # no positive kwh
        assert q3["mwh_total"] == 0


class TestReportsSent:
    def test_delivery_after_quarter_end_marks_sent(self, client):
        """Bills in Q4-2025, delivery recorded in 2026 → 'sent'."""
        tid, auth = _make_tenant()
        delivery_ts = datetime(2026, 1, 20)
        cid = _add_client(tid, last_delivery_at=delivery_ts)
        aid = _add_array(tid, cid)
        uid = _add_account(tid, aid)
        _add_bill(uid, tid, date(2025, 10, 1), kwh=8_000)

        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        q4 = next(r for r in reports if r["quarter"] == "Q4-2025")
        assert q4["status"] == "sent"
        assert q4["last_delivered_at"] is not None

    def test_delivery_before_quarter_end_not_sent(self, client):
        """Delivery in 2025-11 can't cover Q4-2025 (ends 2025-12-31) → 'ready'."""
        tid, auth = _make_tenant()
        delivery_ts = datetime(2025, 11, 15)   # before Q4-2025 ends
        cid = _add_client(tid, last_delivery_at=delivery_ts)
        aid = _add_array(tid, cid)
        uid = _add_account(tid, aid)
        _add_bill(uid, tid, date(2025, 10, 1), kwh=8_000)

        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]
        q4 = next(r for r in reports if r["quarter"] == "Q4-2025")
        assert q4["status"] == "ready"


class TestReportsMixed:
    def test_mixed_statuses_across_six_quarters(self, client):
        """Realistic scenario: Q2-2026 draft, Q1-2026 ready, Q4-2025 sent."""
        tid, auth = _make_tenant()
        delivery_ts = datetime(2026, 2, 1)   # after Q4-2025 ends
        cid = _add_client(tid, last_delivery_at=delivery_ts)
        aid = _add_array(tid, cid)
        uid = _add_account(tid, aid)

        # Q1-2026 data (no delivery for it yet)
        for m in (1, 2, 3):
            _add_bill(uid, tid, date(2026, m, 1), kwh=5_000)
        # Q4-2025 data (covered by delivery_ts)
        for m in (10, 11, 12):
            _add_bill(uid, tid, date(2025, m, 1), kwh=5_000)

        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        reports = resp.json()["reports"]

        by_q = {r["quarter"]: r for r in reports}
        assert by_q["Q2-2026"]["status"] == "draft"   # in progress
        assert by_q["Q1-2026"]["status"] == "ready"   # data, no delivery after Mar 31 2026
        assert by_q["Q4-2025"]["status"] == "sent"    # delivery on Jan 20 2026 > Dec 31 2025
        # Quarters without bills are draft (arrays exist)
        assert by_q["Q3-2025"]["status"] == "draft"

    def test_response_shape(self, client):
        """Every report object has the required fields."""
        _, auth = _make_tenant()
        resp = _get_reports(client, auth)
        assert resp.status_code == 200
        for r in resp.json()["reports"]:
            for field in ("quarter", "year", "quarter_num", "status",
                          "array_count", "last_generated_at",
                          "last_delivered_at", "mwh_total"):
                assert field in r, f"missing field {field!r} in report"


class TestDownloadWithQuarter:
    def test_download_no_quarter(self, client):
        """Existing behavior: no quarter param returns the current rolling xlsx."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        _add_array(tid, cid)
        resp = client.get(
            f"/v1/account/clients/{cid}/report.xlsx",
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]

    def test_download_with_valid_quarter(self, client):
        """quarter=Q1-2026 returns an xlsx scoped to Q1-2026 window."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        _add_array(tid, cid)
        resp = client.get(
            f"/v1/account/clients/{cid}/report.xlsx?quarter=Q1-2026",
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]
        assert "Q1-2026" in resp.headers.get("content-disposition", "")

    def test_download_invalid_quarter_returns_400(self, client):
        """Malformed quarter string → 400."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        resp = client.get(
            f"/v1/account/clients/{cid}/report.xlsx?quarter=bad",
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400


class TestRegenerate:
    def test_regenerate_all_clients(self, client):
        """POST /v1/account/regenerate with no body regenerates all active clients."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        _add_array(tid, cid)
        resp = client.post(
            "/v1/account/regenerate",
            json={},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "regenerated"
        assert "generated_at" in body

    def test_regenerate_specific_client(self, client):
        """POST /v1/account/regenerate with client_id regenerates just that client."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        _add_array(tid, cid)
        resp = client.post(
            "/v1/account/regenerate",
            json={"client_id": cid},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "regenerated"

    def test_regenerate_with_quarter(self, client):
        """quarter param is accepted and scopes the window."""
        tid, auth = _make_tenant()
        cid = _add_client(tid)
        _add_array(tid, cid)
        resp = client.post(
            "/v1/account/regenerate",
            json={"quarter": "Q1-2026"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "regenerated"

    def test_regenerate_bad_quarter_returns_400(self, client):
        """Malformed quarter → 400."""
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/regenerate",
            json={"quarter": "Q5-2026"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400

    def test_regenerate_wrong_client_returns_404(self, client):
        """Client belonging to another tenant → 404."""
        tid1, auth1 = _make_tenant()
        tid2, _ = _make_tenant()
        cid2 = _add_client(tid2)
        resp = client.post(
            "/v1/account/regenerate",
            json={"client_id": cid2},
            headers={"Authorization": auth1},
        )
        assert resp.status_code == 404


class TestQuarterParseHelper:
    def test_valid_formats(self):
        from api.account import _parse_quarter_str
        assert _parse_quarter_str("Q1-2026") == (2026, 1)
        assert _parse_quarter_str("Q4-2025") == (2025, 4)
        assert _parse_quarter_str("q2 2024") == (2024, 2)

    def test_invalid_formats(self):
        from api.account import _parse_quarter_str
        import pytest
        for bad in ("Q5-2026", "2026-Q1", "Q1/2026", "Q0-2026", "bad"):
            with pytest.raises(ValueError):
                _parse_quarter_str(bad)

    def test_quarter_end_date(self):
        from api.account import _quarter_end_date
        from datetime import date
        assert _quarter_end_date(2026, 1) == date(2026, 3, 31)
        assert _quarter_end_date(2026, 2) == date(2026, 6, 30)
        assert _quarter_end_date(2026, 3) == date(2026, 9, 30)
        assert _quarter_end_date(2025, 4) == date(2025, 12, 31)

    def test_quarter_to_reference_date(self):
        from api.account import _quarter_to_reference_date
        from datetime import date
        assert _quarter_to_reference_date(2026, 1) == date(2026, 4, 1)
        assert _quarter_to_reference_date(2025, 4) == date(2026, 1, 1)
        assert _quarter_to_reference_date(2026, 3) == date(2026, 10, 1)
