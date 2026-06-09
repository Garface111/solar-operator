"""
Tests for soft-delete visibility and restore in the array endpoints.

Covers:
  A. GET .../arrays?include_deleted=true returns soft-deleted arrays with deleted_at
  B. GET .../arrays (default) hides soft-deleted — regression guard for ArrayList
  C. POST .../restore on freshly-deleted array → 200, deleted_at cleared
  D. POST .../restore on never-deleted array → 200 idempotent
  E. POST .../restore on >30-day-old soft-delete → 410 with structured error
  F. Cross-tenant restore attempt → 404 (no info leak)
  G. GET /v1/sandbox/canvas includes recently soft-deleted array with array_deleted_at
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import select

from api.account import mint_session_for_tenant, HARD_DELETE_GRACE_DAYS
from api.db import SessionLocal
from api.models import Array, Client, Tenant, UtilityAccount


def _make_tenant() -> tuple[str, str]:
    """Create a fresh tenant; return (tenant_id, 'Bearer <session_token>')."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="SoftDelete Test", contact_email=f"{tid}@test.com",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _make_client(tid: str, name: str = "Test Client") -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name=name, active=True)
        db.add(c)
        db.commit()
        return c.id


def _make_array(tid: str, client_id: int, name: str = "Test Array",
                deleted_at: datetime | None = None) -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, client_id=client_id, name=name, deleted_at=deleted_at)
        db.add(a)
        db.commit()
        return a.id


def _make_ua(tid: str, array_id: int, account_number: str = "1234-5678",
             deleted_at: datetime | None = None) -> int:
    with SessionLocal() as db:
        u = UtilityAccount(
            tenant_id=tid, array_id=array_id,
            provider="gmp", account_number=account_number,
            nickname="Test Account", deleted_at=deleted_at,
        )
        db.add(u)
        db.commit()
        return u.id


# ── A. include_deleted=true returns soft-deleted arrays with deleted_at ────────

def test_include_deleted_returns_soft_deleted(client):
    tid, auth = _make_tenant()
    c_id = _make_client(tid)
    now_ts = datetime.utcnow()
    _make_array(tid, c_id, "Live Array")
    del_id = _make_array(tid, c_id, "Deleted Array", deleted_at=now_ts - timedelta(days=3))

    resp = client.get(
        f"/v1/account/clients/{c_id}/arrays?include_deleted=true",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    arrays = resp.json()["arrays"]
    names = {a["name"] for a in arrays}
    assert "Live Array" in names
    assert "Deleted Array" in names

    del_row = next(a for a in arrays if a["name"] == "Deleted Array")
    assert del_row["deleted_at"] is not None
    assert del_row["id"] == del_id


# ── B. Default hides soft-deleted (regression guard for ArrayList) ─────────────

def test_default_hides_soft_deleted(client):
    tid, auth = _make_tenant()
    c_id = _make_client(tid)
    _make_array(tid, c_id, "Live Array B")
    _make_array(tid, c_id, "Hidden Array", deleted_at=datetime.utcnow() - timedelta(days=1))

    resp = client.get(
        f"/v1/account/clients/{c_id}/arrays",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    arrays = resp.json()["arrays"]
    names = [a["name"] for a in arrays]
    assert "Live Array B" in names
    assert "Hidden Array" not in names


# ── C. Restore freshly-deleted array → 200, deleted_at cleared ────────────────

def test_restore_freshly_deleted(client):
    tid, auth = _make_tenant()
    c_id = _make_client(tid)
    now_ts = datetime.utcnow()
    arr_id = _make_array(tid, c_id, "To Restore", deleted_at=now_ts - timedelta(hours=1))
    _make_ua(tid, arr_id, "9999-0001", deleted_at=now_ts - timedelta(hours=1))

    resp = client.post(
        f"/v1/account/clients/{c_id}/arrays/{arr_id}/restore",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["array"]["deleted_at"] is None

    # DB check
    with SessionLocal() as db:
        a = db.get(Array, arr_id)
        assert a.deleted_at is None
        ua = db.execute(
            select(UtilityAccount).where(UtilityAccount.array_id == arr_id)
        ).scalar_one()
        assert ua.deleted_at is None


# ── D. Restore on never-deleted → 200 idempotent ──────────────────────────────

def test_restore_idempotent_when_active(client):
    tid, auth = _make_tenant()
    c_id = _make_client(tid)
    arr_id = _make_array(tid, c_id, "Already Active")

    resp = client.post(
        f"/v1/account/clients/{c_id}/arrays/{arr_id}/restore",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["array"]["deleted_at"] is None


# ── E. Restore on >30-day-old soft-delete → 410 ───────────────────────────────

def test_restore_expired_returns_410(client):
    tid, auth = _make_tenant()
    c_id = _make_client(tid)
    old_ts = datetime.utcnow() - timedelta(days=HARD_DELETE_GRACE_DAYS + 1)
    arr_id = _make_array(tid, c_id, "Ancient Array", deleted_at=old_ts)

    resp = client.post(
        f"/v1/account/clients/{c_id}/arrays/{arr_id}/restore",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 410
    assert resp.json()["detail"]["error"] == "purge-window-elapsed"


# ── F. Cross-tenant restore → 404 (no info leak) ──────────────────────────────

def test_restore_cross_tenant_returns_404(client):
    tid_a, auth_a = _make_tenant()
    tid_b, _ = _make_tenant()
    c_a = _make_client(tid_a, "Owner Client")
    c_b = _make_client(tid_b, "Other Client")
    arr_id = _make_array(tid_a, c_a, "Private Array",
                         deleted_at=datetime.utcnow() - timedelta(hours=1))

    # Tenant B tries to restore tenant A's array via their own client_id
    resp = client.post(
        f"/v1/account/clients/{c_b}/arrays/{arr_id}/restore",
        headers={"Authorization": _make_tenant()[1]},  # fresh tenant C session
    )
    assert resp.status_code == 404


# ── G. Canvas includes recently deleted array with array_deleted_at ────────────

def test_canvas_includes_soft_deleted_array(client):
    tid, auth = _make_tenant()
    c_id = _make_client(tid, "Canvas Client")
    now_ts = datetime.utcnow()
    arr_id = _make_array(tid, c_id, "Ghost Array",
                         deleted_at=now_ts - timedelta(days=5))
    _make_ua(tid, arr_id, "0000-1111", deleted_at=now_ts - timedelta(days=5))

    resp = client.get("/v1/sandbox/canvas", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()

    # The client should appear (it's not deleted)
    client_out = next((c for c in data["clients"] if c["id"] == c_id), None)
    assert client_out is not None, "canvas client missing"

    # The ghost array's account should appear with array_deleted_at set
    ghost_accs = [
        a for a in client_out["accounts"]
        if a.get("array_id") == arr_id
    ]
    assert len(ghost_accs) == 1
    assert ghost_accs[0]["array_deleted_at"] is not None


# ── H. Soft-deleted account with no client to ghost under is NOT unclassified ──

def test_soft_deleted_orphan_account_not_unclassified(client):
    """Regression (Ford, Jun 9 '26): deleting arrays must not leave behind a
    floating 'Drag onto a client card to attach' card.

    A soft-deleted UtilityAccount whose array is detached from any client (or
    has a null array_id) has nowhere to render as a ghost row, so it must be
    DROPPED from the canvas — never promoted into the `unclassified` bucket.
    """
    tid, auth = _make_tenant()
    now_ts = datetime.utcnow()

    # Case 1: soft-deleted account with array_id = None (fully orphaned)
    with SessionLocal() as db:
        u_null = UtilityAccount(
            tenant_id=tid, array_id=None, provider="gmp",
            account_number="4392604400", nickname="Orphan",
            deleted_at=now_ts - timedelta(minutes=1),
        )
        db.add(u_null)
        db.commit()
        u_null_id = u_null.id

    # Case 2: soft-deleted account whose array is deleted AND has no client
    clientless_arr = _make_array(tid, None, "Clientless",  # type: ignore[arg-type]
                                 deleted_at=now_ts - timedelta(minutes=1))
    u_detached_id = _make_ua(tid, clientless_arr, "9999-0000",
                             deleted_at=now_ts - timedelta(minutes=1))

    resp = client.get("/v1/sandbox/canvas", headers={"Authorization": auth})
    assert resp.status_code == 200
    data = resp.json()

    uncl_ids = {a["id"] for a in data["unclassified"]}
    assert u_null_id not in uncl_ids, "fully-orphaned soft-deleted account leaked as a card"
    assert u_detached_id not in uncl_ids, "detached soft-deleted account leaked as a card"

    # And a LIVE orphan (array_id None, not deleted) DOES still appear — the
    # legitimate "needs attaching" case must keep working.
    with SessionLocal() as db:
        u_live = UtilityAccount(
            tenant_id=tid, array_id=None, provider="gmp",
            account_number="1111-2222", nickname="LiveOrphan",
        )
        db.add(u_live)
        db.commit()
        u_live_id = u_live.id

    resp2 = client.get("/v1/sandbox/canvas", headers={"Authorization": auth})
    uncl_ids2 = {a["id"] for a in resp2.json()["unclassified"]}
    assert u_live_id in uncl_ids2, "live unattached account should still show as a card"
