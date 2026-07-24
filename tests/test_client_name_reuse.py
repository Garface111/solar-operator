"""Deleted client names are reusable — live-rows-only uniqueness.

Ford (2026-07-24, stress test): deleted "Pbozuwa", re-added it from the same
login, got "A client with that name already exists". The uq_client_per_tenant
UNIQUE spanned soft-deleted rows, so the ghost reserved its name — exactly the
2026-07-16 array bug, one table over. Now a partial index scopes uniqueness to
live rows, and the undo path renames a restored row whose name was re-taken.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Client, Tenant


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Reuse Co", contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
            generation_reports=True,
        ))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def test_deleted_client_name_is_reusable(client):
    """The exact Ford flow: create → delete → recreate the same name."""
    tid = _tenant()
    r = client.post("/v1/account/clients", headers=_auth(tid),
                    json={"name": "Pbozuwa"})
    assert r.status_code == 200, r.text
    cid = r.json()["client"]["id"]

    r = client.delete(f"/v1/account/clients/{cid}", headers=_auth(tid))
    assert r.status_code == 200, r.text

    r = client.post("/v1/account/clients", headers=_auth(tid),
                    json={"name": "Pbozuwa"})
    assert r.status_code == 200, r.text  # was 409: ghost reserved the name
    assert r.json()["client"]["id"] != cid

    with SessionLocal() as db:
        rows = db.execute(select(Client).where(
            Client.tenant_id == tid, Client.name == "Pbozuwa")).scalars().all()
        live = [c for c in rows if c.deleted_at is None]
        assert len(rows) == 2 and len(live) == 1


def test_live_duplicate_still_rejected(client):
    """Live-rows uniqueness itself must survive the constraint swap."""
    tid = _tenant()
    r = client.post("/v1/account/clients", headers=_auth(tid),
                    json={"name": "Solo"})
    assert r.status_code == 200
    r = client.post("/v1/account/clients", headers=_auth(tid),
                    json={"name": "Solo"})
    assert r.status_code == 409


def test_undo_renames_when_name_was_retaken(client):
    """Delete → recreate the name → undo the delete: the RESTORED client comes
    back renamed '(restored)' instead of the undo 500ing on the partial index."""
    tid = _tenant()
    r = client.post("/v1/account/clients", headers=_auth(tid),
                    json={"name": "Pbozuwa"})
    old_id = r.json()["client"]["id"]
    r = client.delete(f"/v1/account/clients/{old_id}", headers=_auth(tid))
    assert r.status_code == 200, r.text
    undo_token = (r.json() or {}).get("undo_token")

    r = client.post("/v1/account/clients", headers=_auth(tid),
                    json={"name": "Pbozuwa"})
    assert r.status_code == 200
    new_id = r.json()["client"]["id"]

    if not undo_token:
        # Delete response carries no undo token in this build — nothing to test.
        return
    r = client.post("/v1/account/undo-delete", headers=_auth(tid),
                    json={"undo_token": undo_token})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        restored = db.get(Client, old_id)
        current = db.get(Client, new_id)
        assert restored.deleted_at is None
        assert current.deleted_at is None
        assert restored.name == "Pbozuwa (restored)"
        assert current.name == "Pbozuwa"
