"""GET /v1/account/clients/{id}/pull-status — the UI's "data is arriving" feed.

Born from Ford (2026-07-24) watching an empty client for 46s while a 9-year
Locus history pull ran with zero indication. pending/error/done per
vendor-connected array, with days + range so "9 years loaded" is showable.
"""
from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import select  # noqa: F401 — parity with sibling test modules

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (
    Array,
    Client,
    DailyGeneration,
    InverterConnection,
    Tenant,
    now,
)


def _tenant_and_client() -> tuple[str, int]:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Pull Co", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
            generation_reports=True,
        ))
        c = Client(tenant_id=tid, name="Puller", active=True)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    return tid, cid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def _array_with_conn(tid: str, cid: int, name: str, *, stamped: bool,
                     error: str | None = None, days: int = 0) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, client_id=cid, name=name, fuel_type="solar")
        db.add(arr)
        db.flush()
        db.add(InverterConnection(
            array_id=arr.id, vendor="locus",
            config={"username": "u", "password": "p", "site_id": arr.id},
            status="ok",
            history_backfilled_at=now() if stamped else None,
            last_error=error,
        ))
        for i in range(days):
            db.add(DailyGeneration(
                tenant_id=tid, array_id=arr.id,
                day=date(2026, 1, 1 + i), kwh=10.0, source="locus",
            ))
        aid = arr.id
        db.commit()
    return aid


def test_pull_status_reports_pending_error_done(client):
    tid, cid = _tenant_and_client()
    a_pending = _array_with_conn(tid, cid, "Pending Site", stamped=False, days=2)
    a_error = _array_with_conn(tid, cid, "Errored Site", stamped=True,
                               error="history backfill: years 2019 failed: 503",
                               days=5)
    a_done = _array_with_conn(tid, cid, "Done Site", stamped=True, days=3)

    r = client.get(f"/v1/account/clients/{cid}/pull-status", headers=_auth(tid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pulling"] == 1
    by_id = {a["array_id"]: a for a in body["arrays"]}
    assert by_id[a_pending]["status"] == "pending"
    assert by_id[a_pending]["days"] == 2
    assert by_id[a_error]["status"] == "error"
    assert "2019" in by_id[a_error]["error"]
    assert by_id[a_done]["status"] == "done"
    assert by_id[a_done]["first_day"] == "2026-01-01"


def test_pull_status_is_tenant_scoped(client):
    tid, cid = _tenant_and_client()
    other, _ = _tenant_and_client()
    r = client.get(f"/v1/account/clients/{cid}/pull-status", headers=_auth(other))
    assert r.status_code == 404
