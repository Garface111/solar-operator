"""
Tests for PATCH /v1/account/clients/{client_id}:
  - contact_email round-trip (today's user-reported bug)
  - contact_email survives a /v1/sync of a *different* client (regression guard)
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant, UtilityAccount


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_tenant() -> tuple[str, str, str]:
    """Create a fresh tenant; return (tenant_id, tenant_key, bearer_header)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(16)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Client PATCH Test",
            contact_email=f"{tid}@patch.test",
            tenant_key=key,
            plan="standard",
            active=True,
        ))
        db.commit()
    session_auth = f"Bearer {mint_session_for_tenant(tid)}"
    return tid, key, session_auth


def _make_client(tenant_id: str, name: str, *,
                 gmp_email: str | None = None,
                 contact_email: str | None = None,
                 gmp_autopopulate: bool = True) -> int:
    with SessionLocal() as db:
        c = Client(
            tenant_id=tenant_id,
            name=name,
            gmp_email=gmp_email,
            contact_email=contact_email,
            gmp_autopopulate=gmp_autopopulate,
            active=True,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def _gmp_sync_payload(gmp_email: str, account_number: str = "ACC001") -> dict:
    return {
        "provider": "gmp",
        "user": {"email": gmp_email},
        "auth": {"apiToken": "tok_test"},
        "accounts": [{"accountNumber": account_number, "nickname": "Test Array"}],
    }


# ── contact_email PATCH round-trip ────────────────────────────────────────────


def test_patch_contact_email_persists(client):
    tid, _, auth = _make_tenant()
    cid = _make_client(tid, "River Farm")

    new_email = "riverfarm@example.com"
    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"contact_email": new_email},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["client"]["contact_email"] == new_email

    # Verify directly in DB
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.contact_email == new_email


def test_patch_contact_email_returned_in_response(client):
    """The updated contact_email is echoed back in the PATCH response."""
    tid, _, auth = _make_tenant()
    cid = _make_client(tid, "Valley Solar")

    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"contact_email": "valley@solar.com"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["client"]["contact_email"] == "valley@solar.com"


def test_patch_other_fields_do_not_clear_contact_email(client):
    """Patching an unrelated field (notes) leaves contact_email intact."""
    tid, _, auth = _make_tenant()
    cid = _make_client(tid, "Hilltop Co-op",
                       contact_email="hilltop@coop.com")

    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"notes": "Some notes update"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["client"]["contact_email"] == "hilltop@coop.com"


# ── contact_email survives sync of a different client ─────────────────────────


def test_contact_email_preserved_after_different_client_sync(client):
    """
    Regression: Client A has a manually-set contact_email. A /v1/sync for
    Client B (different gmp_email) must not touch Client A's contact_email.

    This reproduces the user-reported bug where the sandbox contact_email edit
    appeared to be lost after the extension re-synced a different account.
    """
    tid, key, session_auth = _make_tenant()

    # Client A: user manually set contact_email via sandbox edit
    cid_a = _make_client(tid, "Client Alpha",
                         gmp_email="alpha@gmp.test",
                         contact_email="owner.alpha@private.com",
                         gmp_autopopulate=True)

    # Client B: a separate client with a different GMP login
    cid_b = _make_client(tid, "Client Beta",
                         gmp_email="beta@gmp.test",
                         contact_email=None,
                         gmp_autopopulate=True)

    # Simulate extension syncing Client B's portal account
    sync_payload = _gmp_sync_payload("beta@gmp.test", account_number="BETAACC001")
    sync_resp = client.post(
        "/v1/sync",
        json=sync_payload,
        headers={"Authorization": f"Bearer {key}"},
    )
    assert sync_resp.status_code == 200, sync_resp.text

    # Client A's contact_email must still be intact
    with SessionLocal() as db:
        c_a = db.get(Client, cid_a)
        assert c_a.contact_email == "owner.alpha@private.com", (
            "Client A's contact_email was wiped by a sync of Client B"
        )


def test_contact_email_patch_then_sync_same_client_preserved(client):
    """
    After PATCHing contact_email on a client, re-syncing that same client
    (which auto-populates arrays) must NOT overwrite contact_email.
    """
    tid, key, session_auth = _make_tenant()

    # Create Client with gmp_email but no contact_email initially
    cid = _make_client(tid, "Sync Test Client",
                       gmp_email="synctest@gmp.test",
                       gmp_autopopulate=True)

    # Manually set contact_email via PATCH
    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"contact_email": "manually.set@example.com"},
        headers={"Authorization": session_auth},
    )
    assert resp.status_code == 200, resp.text

    # Now re-sync this same client's GMP portal
    sync_payload = _gmp_sync_payload("synctest@gmp.test", account_number="STA001")
    sync_resp = client.post(
        "/v1/sync",
        json=sync_payload,
        headers={"Authorization": f"Bearer {key}"},
    )
    assert sync_resp.status_code == 200, sync_resp.text

    # contact_email must be unchanged after the sync
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.contact_email == "manually.set@example.com", (
            "Sync overwrote manually-set contact_email"
        )
