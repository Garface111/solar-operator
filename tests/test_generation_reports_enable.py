"""POST /v1/account/generation-reports/enable — open the gen-reports desk.

AO tenants need a path into the reports world that does NOT flip every client's
auto_send or start $15/array/quarter billing. auto-send-all remains the
deliberate enroll-everyone action; this endpoint only sets
Tenant.generation_reports = True.
"""
from __future__ import annotations

import secrets

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Client, Tenant


def _mk_tenant(*, product: str = "array_operator", gen_reports: bool = False,
               is_demo: bool = False, status: str = "active") -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name=f"GenEnable {tid[-4:]}",
            contact_email=f"{tid}@genenable.test",
            tenant_key="k_" + secrets.token_hex(8),
            plan="standard",
            active=True,
            product=product,
            is_demo=is_demo,
            subscription_status=status,
            generation_reports=gen_reports,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _mk_client(tid: str, *, auto_send: bool = False) -> int:
    with SessionLocal() as db:
        c = Client(
            tenant_id=tid,
            name="Cl " + secrets.token_hex(3),
            active=True,
            contact_email="client@genenable.test",
            auto_send=auto_send,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def _flag(tid: str) -> bool:
    with SessionLocal() as db:
        return bool(db.get(Tenant, tid).generation_reports)


def _client_auto_send(cid: int) -> bool:
    with SessionLocal() as db:
        return bool(db.get(Client, cid).auto_send)


def test_enable_flips_generation_reports(client: TestClient):
    tid, auth = _mk_tenant(gen_reports=False)
    assert _flag(tid) is False

    resp = client.post(
        "/v1/account/generation-reports/enable",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["generation_reports"] is True
    assert "auto-send" in body["message"].lower() or "auto_send" in body["message"].lower() \
        or "per-client" in body["message"].lower()
    assert _flag(tid) is True


def test_enable_idempotent(client: TestClient):
    tid, auth = _mk_tenant(gen_reports=True)
    resp = client.post(
        "/v1/account/generation-reports/enable",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["generation_reports"] is True
    assert _flag(tid) is True


def test_enable_does_not_flip_auto_send(client: TestClient):
    tid, auth = _mk_tenant(gen_reports=False)
    off = _mk_client(tid, auto_send=False)
    on = _mk_client(tid, auto_send=True)

    resp = client.post(
        "/v1/account/generation-reports/enable",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert _flag(tid) is True
    assert _client_auto_send(off) is False
    assert _client_auto_send(on) is True

    # No client row should have been mass-enrolled.
    with SessionLocal() as db:
        rows = db.execute(
            select(Client.auto_send).where(Client.tenant_id == tid)
        ).scalars().all()
    assert sorted(rows) == [False, True]


def test_enable_unlocks_get_account_flag(client: TestClient):
    tid, auth = _mk_tenant(product="array_operator", gen_reports=False)
    before = client.get("/v1/account", headers={"Authorization": auth})
    assert before.status_code == 200
    assert before.json()["generation_reports"] is False

    en = client.post(
        "/v1/account/generation-reports/enable",
        headers={"Authorization": auth},
    )
    assert en.status_code == 200

    after = client.get("/v1/account", headers={"Authorization": auth})
    assert after.status_code == 200
    assert after.json()["generation_reports"] is True


def test_enable_rejects_demo(client: TestClient):
    _, auth = _mk_tenant(is_demo=True, gen_reports=False)
    resp = client.post(
        "/v1/account/generation-reports/enable",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 403
    detail = resp.json().get("detail") or {}
    if isinstance(detail, dict):
        assert detail.get("error") == "demo-read-only"


def test_enable_unauth_401(client: TestClient):
    resp = client.post("/v1/account/generation-reports/enable")
    assert resp.status_code == 401

    resp2 = client.post(
        "/v1/account/generation-reports/enable",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp2.status_code == 401


def test_enable_does_not_create_genreport_charges(client: TestClient):
    from api.models import GenReportCharge
    from sqlalchemy import func

    tid, auth = _mk_tenant(gen_reports=False)
    _mk_client(tid, auto_send=False)

    client.post(
        "/v1/account/generation-reports/enable",
        headers={"Authorization": auth},
    )
    with SessionLocal() as db:
        n = int(db.execute(
            select(func.count()).select_from(GenReportCharge)
            .where(GenReportCharge.tenant_id == tid)
        ).scalar() or 0)
    assert n == 0
