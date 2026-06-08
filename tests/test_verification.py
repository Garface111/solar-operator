"""
Smoke tests for the verify-accuracy API.

POST /v1/verification/upload -> detail
GET  /v1/verification?client_id=<id> -> list
GET  /v1/verification/{id} -> detail
POST /v1/verification/{id}/resolve -> resolved detail
"""
from __future__ import annotations

import io
import secrets

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Client, Tenant


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(16)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Verify Test Tenant",
            contact_email=f"{tid}@verify.test",
            tenant_key=key,
            plan="standard",
            active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _make_client(tenant_id: str, name: str) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tenant_id, name=name, active=True)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def test_upload_and_resolve(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Sunny Farm")

    # Upload a tiny CSV
    csv_data = b"month,kwh\n2026-01,12345\n2026-02,11000\n"
    resp = client.post(
        "/v1/verification/upload",
        data={"client_id": cid, "period_label": "Q1 2026"},
        files={"file": ("records.csv", io.BytesIO(csv_data), "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_id"] == cid
    assert body["period_label"] == "Q1 2026"
    assert body["status"] == "pending"
    assert body["uploaded_filename"] == "records.csv"
    check_id = body["id"]

    # List
    resp2 = client.get(
        f"/v1/verification?client_id={cid}",
        headers={"Authorization": auth},
    )
    assert resp2.status_code == 200, resp2.text
    checks = resp2.json()["checks"]
    assert any(c["id"] == check_id for c in checks)

    # Detail
    resp3 = client.get(
        f"/v1/verification/{check_id}",
        headers={"Authorization": auth},
    )
    assert resp3.status_code == 200, resp3.text
    assert resp3.json()["id"] == check_id

    # Resolve as confirmed
    resp4 = client.post(
        f"/v1/verification/{check_id}/resolve",
        json={"status": "confirmed"},
        headers={"Authorization": auth},
    )
    assert resp4.status_code == 200, resp4.text
    resolved = resp4.json()
    assert resolved["status"] == "confirmed"
    assert resolved["resolved_at"] is not None


def test_resolve_flagged_with_note(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Hilltop Solar")

    csv_data = b"month,kwh\n2025-10,9000\n"
    resp = client.post(
        "/v1/verification/upload",
        data={"client_id": cid, "period_label": "Q4 2025"},
        files={"file": ("q4.csv", io.BytesIO(csv_data), "text/csv")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    check_id = resp.json()["id"]

    resp2 = client.post(
        f"/v1/verification/{check_id}/resolve",
        json={"status": "flagged", "note": "October MWh is off by 0.3"},
        headers={"Authorization": auth},
    )
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["status"] == "flagged"
    assert body["operator_note"] == "October MWh is off by 0.3"


def test_upload_wrong_type_rejected(client):
    tid, auth = _make_tenant()
    cid = _make_client(tid, "Wind Ridge")

    resp = client.post(
        "/v1/verification/upload",
        data={"client_id": cid, "period_label": "Q1 2026"},
        files={"file": ("attack.exe", io.BytesIO(b"not a real exe"), "application/octet-stream")},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 400, resp.text


def test_wrong_tenant_cannot_access(client):
    tid1, auth1 = _make_tenant()
    tid2, auth2 = _make_tenant()
    cid1 = _make_client(tid1, "River Farm")

    csv_data = b"month,kwh\n2026-01,1000\n"
    upload = client.post(
        "/v1/verification/upload",
        data={"client_id": cid1, "period_label": "Q1 2026"},
        files={"file": ("r.csv", io.BytesIO(csv_data), "text/csv")},
        headers={"Authorization": auth1},
    )
    assert upload.status_code == 200, upload.text
    check_id = upload.json()["id"]

    # Tenant 2 cannot see tenant 1's check
    resp = client.get(
        f"/v1/verification/{check_id}",
        headers={"Authorization": auth2},
    )
    assert resp.status_code == 404, resp.text
