"""Demo tenant read-only guard.

A demo session can browse (GET) but every mutating endpoint refuses with a
403 / demo-read-only body. A normal tenant is unaffected by the guard.
"""
from __future__ import annotations

import secrets

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant


def _make_tenant(*, is_demo: bool) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Demo Co" if is_demo else "Real Co",
            contact_email=f"{tid}@example.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="demo" if is_demo else "standard",
            subscription_status="demo" if is_demo else "active",
            active=True,
            is_demo=is_demo,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def test_demo_can_get_account(client):
    tid, auth = _make_tenant(is_demo=True)
    resp = client.get("/v1/account", headers={"Authorization": auth})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == tid
    assert body["is_demo"] is True


def test_demo_cannot_create_client(client):
    _, auth = _make_tenant(is_demo=True)
    resp = client.post(
        "/v1/account/clients",
        json={"name": "Should Not Persist"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "demo-read-only"
    assert detail["cta_url"] == "/signup"
    assert "sign up" in detail["message"].lower()


def test_demo_cannot_delete_or_patch(client):
    _, auth = _make_tenant(is_demo=True)
    # PATCH and DELETE on tenant-owned resources are equally refused.
    patch = client.patch(
        "/v1/account/clients/1",
        json={"name": "x"},
        headers={"Authorization": auth},
    )
    assert patch.status_code == 403
    assert patch.json()["detail"]["error"] == "demo-read-only"

    delete = client.delete(
        "/v1/account/clients/1",
        headers={"Authorization": auth},
    )
    assert delete.status_code == 403
    assert delete.json()["detail"]["error"] == "demo-read-only"


def test_normal_tenant_not_blocked(client):
    """Control: the guard is a no-op for real tenants — a write succeeds."""
    _, auth = _make_tenant(is_demo=False)
    resp = client.post(
        "/v1/account/clients",
        json={"name": "Acme Solar Co-op"},
        headers={"Authorization": auth},
    )
    assert resp.status_code != 403
    assert resp.status_code < 500


def test_normal_account_is_demo_false(client):
    tid, auth = _make_tenant(is_demo=False)
    resp = client.get("/v1/account", headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.json()["is_demo"] is False
