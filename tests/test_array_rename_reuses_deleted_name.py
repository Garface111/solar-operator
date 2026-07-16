"""Array-name uniqueness must IGNORE soft-deleted rows (2026-07-16).

Bruce (NEPOOL Operator) deleted "sibling" arrays (e.g. SolarEdge-captured
twins) and then could no longer rename a live array to that clean name: the
old uq_array_per_tenant UNIQUE(tenant_id, name) constraint spanned soft-deleted
rows, so the ghost still reserved the name → 409 / 500. Uniqueness is now a
PARTIAL index over live rows (deleted_at IS NULL), so a deleted array's name is
freely reusable while two LIVE arrays still may not collide.
"""
from __future__ import annotations

import secrets

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant, now


def _active_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="RenameReuse", contact_email=f"{tid}@test.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(18),
            plan="standard", active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _client(tid: str) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="C-" + secrets.token_hex(3), active=True)
        db.add(c); db.flush()
        cid = c.id
        db.commit()
    return cid


def _make_array(cid: int, tid: str, auth, name: str, client) -> int:
    r = client.post(f"/v1/account/clients/{cid}/arrays",
                    headers={"Authorization": auth}, json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["array"]["id"]


def _soft_delete(aid: int) -> None:
    with SessionLocal() as db:
        a = db.get(Array, aid)
        a.deleted_at = now()
        db.commit()


def test_rename_live_array_to_a_deleted_arrays_name_succeeds(client):
    """The exact Bruce bug: delete 'Waterford', then rename a live array to it."""
    tid, auth = _active_tenant()
    cid = _client(tid)

    ghost_id = _make_array(cid, tid, auth, "Waterford", client)
    _soft_delete(ghost_id)                       # deleted "sibling" leaves a ghost

    live_id = _make_array(cid, tid, auth, "Old SolarEdge Name", client)
    r = client.patch(f"/v1/account/clients/{cid}/arrays/{live_id}",
                     headers={"Authorization": auth}, json={"name": "Waterford"})
    assert r.status_code == 200, r.text          # was 409 before the fix
    assert r.json()["array"]["name"] == "Waterford"

    with SessionLocal() as db:
        assert db.get(Array, live_id).name == "Waterford"
        assert db.get(Array, live_id).deleted_at is None
        assert db.get(Array, ghost_id).deleted_at is not None   # ghost untouched


def test_create_array_with_a_deleted_arrays_name_succeeds(client):
    tid, auth = _active_tenant()
    cid = _client(tid)
    ghost_id = _make_array(cid, tid, auth, "Chester", client)
    _soft_delete(ghost_id)

    r = client.post(f"/v1/account/clients/{cid}/arrays",
                    headers={"Authorization": auth}, json={"name": "Chester"})
    assert r.status_code == 200, r.text          # deleted twin no longer blocks


def test_two_live_arrays_still_cannot_share_a_name(client):
    """Uniqueness among LIVE rows is preserved — the guard didn't disappear."""
    tid, auth = _active_tenant()
    cid = _client(tid)
    _make_array(cid, tid, auth, "Londonderry", client)
    other = _make_array(cid, tid, auth, "Temp", client)

    r = client.patch(f"/v1/account/clients/{cid}/arrays/{other}",
                     headers={"Authorization": auth}, json={"name": "Londonderry"})
    assert r.status_code == 409, r.text
