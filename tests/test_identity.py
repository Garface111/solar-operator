"""
Tests for feat/identity-and-master-account:
  - Smart name splitting at capture (email login → holder name or local-part)
  - Operator-edited client.name preserved across re-capture
  - Merge → undo round-trip (arrays + utility accounts restored)
"""
from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, DeleteHistory, Tenant, UtilityAccount


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tenant() -> tuple[str, str, str]:
    """Return (tenant_id, tenant_key, bearer_header)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(12)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Identity Test Tenant",
            contact_email=f"{tid}@identity.test",
            tenant_key=key,
            plan="standard",
            active=True,
        ))
        db.commit()
    return tid, key, f"Bearer {mint_session_for_tenant(tid)}"


def _make_client(tid: str, name: str, *, gmp_email: str | None = None) -> int:
    with SessionLocal() as db:
        c = Client(
            tenant_id=tid,
            name=name,
            gmp_email=gmp_email,
            gmp_autopopulate=bool(gmp_email),
            active=True,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def _gmp_sync(
    client: TestClient,
    key: str,
    *,
    email: str = "user@example.com",
    account_number: str = "ACC001",
    holder_name: str | None = None,
    nickname: str = "Test Array",
) -> dict:
    user: dict = {"email": email}
    if holder_name:
        user["name"] = holder_name
    return client.post(
        "/v1/sync",
        json={
            "provider": "gmp",
            "user": user,
            "auth": {"apiToken": "tok_test"},
            "accounts": [{"accountNumber": account_number, "nickname": nickname, "solarNetMeter": True}],
        },
        headers={"Authorization": f"Bearer {key}"},
    ).json()


# ── smart-name tests ─────────────────────────────────────────────────────────


def test_email_login_uses_holder_name(client: TestClient):
    """When account_holder_name is present, use it for client.name."""
    tid, key, _ = _make_tenant()

    _gmp_sync(client, key, email="john.doe@gmp.test", holder_name="John Doe", account_number="ACC100")

    with SessionLocal() as db:
        c = db.execute(
            select(Client).where(Client.tenant_id == tid)
        ).scalar_one_or_none()
    assert c is not None
    assert c.name == "John Doe", f"expected 'John Doe', got {c.name!r}"


def test_email_login_no_holder_name_uses_local_part(client: TestClient):
    """Without a holder name, email local-part is de-dotted and title-cased."""
    tid, key, _ = _make_tenant()

    _gmp_sync(client, key, email="mary.smith@gmp.test", account_number="ACC200")

    with SessionLocal() as db:
        c = db.execute(
            select(Client).where(Client.tenant_id == tid)
        ).scalar_one_or_none()
    assert c is not None
    # "mary.smith" → "Mary Smith"
    assert c.name == "Mary Smith", f"expected 'Mary Smith', got {c.name!r}"


def test_email_login_never_raw_email_as_name(client: TestClient):
    """The raw email should not end up as client.name."""
    tid, key, _ = _make_tenant()

    _gmp_sync(client, key, email="rawuser@gmp.test", account_number="ACC300")

    with SessionLocal() as db:
        c = db.execute(
            select(Client).where(Client.tenant_id == tid)
        ).scalar_one_or_none()
    assert c is not None
    assert "@" not in c.name, f"raw email leaked into client.name: {c.name!r}"


def test_contact_email_populated_from_login_email(client: TestClient):
    """If login is an email and contact_email is empty, populate it."""
    tid, key, _ = _make_tenant()

    _gmp_sync(client, key, email="contact@gmp.test", account_number="ACC400")

    with SessionLocal() as db:
        c = db.execute(
            select(Client).where(Client.tenant_id == tid)
        ).scalar_one_or_none()
    assert c is not None
    assert c.contact_email == "contact@gmp.test"


def test_captured_client_name_stored_on_ua(client: TestClient):
    """captured_client_name on UtilityAccount matches the name given to the client."""
    tid, key, _ = _make_tenant()

    _gmp_sync(client, key, email="alice.jones@gmp.test", holder_name="Alice Jones",
              account_number="ACC500")

    with SessionLocal() as db:
        ua = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.account_number == "ACC500",
            )
        ).scalar_one_or_none()
    assert ua is not None
    assert ua.captured_client_name == "Alice Jones"


# ── operator-edited name preservation ────────────────────────────────────────


def test_operator_edited_name_preserved_on_recapture(client: TestClient):
    """If operator edits client.name via PATCH, re-capture must NOT overwrite it."""
    tid, key, auth = _make_tenant()

    # First capture — creates client with auto name
    _gmp_sync(client, key, email="bob.wilson@gmp.test", account_number="ACC600")

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
        cid = c.id
        assert c.name_edited_at is None  # not yet edited

    # Operator renames via PATCH
    resp = client.patch(
        f"/v1/account/clients/{cid}",
        json={"name": "Bob's Solar Farm"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200

    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.name_edited_at is not None  # stamped by PATCH

    # Re-capture the SAME account with a different holder name in the payload
    _gmp_sync(client, key, email="bob.wilson@gmp.test", holder_name="Robert Wilson",
              account_number="ACC600", nickname="Test Array Bob")

    with SessionLocal() as db:
        c = db.get(Client, cid)
    assert c.name == "Bob's Solar Farm", (
        f"Operator edit was overwritten! Got: {c.name!r}"
    )


# ── merge undo round-trip ─────────────────────────────────────────────────────


def test_merge_returns_undo_token(client: TestClient):
    """merge-into response includes undo_token and merged_client_id."""
    tid, _, auth = _make_tenant()
    src_id = _make_client(tid, "Src Client")
    dst_id = _make_client(tid, "Dst Client")

    resp = client.post(
        f"/v1/account/clients/{src_id}/merge-into",
        json={"dst_client_id": dst_id},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "undo_token" in data
    assert data["merged_client_id"] == src_id


def test_merge_undo_restores_src_client_and_arrays(client: TestClient):
    """Merge → undo reverses: src client is restored, arrays move back."""
    tid, _, auth = _make_tenant()

    # Create src with one array and one utility account
    with SessionLocal() as db:
        src = Client(tenant_id=tid, name="Src Client", active=True)
        db.add(src)
        db.flush()
        arr = Array(tenant_id=tid, client_id=src.id, name="Solar Array 1")
        db.add(arr)
        db.flush()
        db.add(UtilityAccount(
            tenant_id=tid, provider="gmp",
            account_number="UA001", array_id=arr.id,
        ))
        db.commit()
        src_id = src.id
        arr_id = arr.id

    dst_id = _make_client(tid, "Dst Client")

    # Merge src into dst
    resp = client.post(
        f"/v1/account/clients/{src_id}/merge-into",
        json={"dst_client_id": dst_id},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    undo_token = resp.json()["undo_token"]

    # Verify src is soft-deleted and array moved to dst
    with SessionLocal() as db:
        src_after = db.get(Client, src_id)
        arr_after = db.get(Array, arr_id)
        assert src_after.deleted_at is not None, "src should be soft-deleted"
        assert arr_after.client_id == dst_id, "array should be under dst"

    # Undo the merge
    resp2 = client.post(
        "/v1/account/clients/merge-undo",
        json={"undo_token": undo_token},
        headers={"Authorization": auth},
    )
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["ok"] is True
    assert data2["restored_client_id"] == src_id

    # Verify src is restored and array moved back
    with SessionLocal() as db:
        src_restored = db.get(Client, src_id)
        arr_restored = db.get(Array, arr_id)
        assert src_restored.deleted_at is None, "src should be restored"
        assert arr_restored.client_id == src_id, "array should be back under src"


def test_merge_undo_token_single_use(client: TestClient):
    """Undo token cannot be used twice."""
    tid, _, auth = _make_tenant()
    src_id = _make_client(tid, "Src2")
    dst_id = _make_client(tid, "Dst2")

    resp = client.post(
        f"/v1/account/clients/{src_id}/merge-into",
        json={"dst_client_id": dst_id},
        headers={"Authorization": auth},
    )
    token = resp.json()["undo_token"]

    client.post("/v1/account/clients/merge-undo",
                json={"undo_token": token},
                headers={"Authorization": auth})

    resp2 = client.post("/v1/account/clients/merge-undo",
                        json={"undo_token": token},
                        headers={"Authorization": auth})
    assert resp2.status_code == 409
