"""Tests for GET /v1/array-owners/onboarding-status — the finish-setup gate.

Ford's rule: "once you connect GMP the finish-setup banner needs to disappear."
So `complete` (which the frontend uses to HIDE the #gmpGate banner) must be True
the moment a GMP session/account exists — even if no GMP account is linked to an
array yet. Linking is a softer, separate nudge (next_step stays 'link_accounts'),
but it must NOT keep the big "you're not done" banner up.
"""
from __future__ import annotations

import secrets

from api.db import SessionLocal
from api.models import Array, Tenant, UtilityAccount


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Onb Status Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _status(client, key: str):
    r = client.get("/v1/array-owners/onboarding-status",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    return r.json()


def test_no_gmp_not_complete(client):
    """No GMP at all → incomplete, banner shows 'connect_gmp'."""
    tid, key = _make_tenant()
    s = _status(client, key)
    assert s["gmp_connected"] is False
    assert s["complete"] is False
    assert s["next_step"] == "connect_gmp"


def test_gmp_connected_but_unlinked_is_complete(client):
    """THE FIX: GMP connected (account captured) but NOT linked to an array →
    complete=True so the finish-setup banner DISAPPEARS. next_step still nudges
    to link, but the big setup gate is gone."""
    tid, key = _make_tenant()
    with SessionLocal() as db:
        # A captured GMP account with NO array_id (unlinked) makes gmp_connected
        # true on its own (gmp_connected = sessions>0 OR accounts>0).
        db.add(UtilityAccount(
            tenant_id=tid, provider="gmp",
            account_number="gmp_" + secrets.token_hex(3), array_id=None,
        ))
        db.commit()

    s = _status(client, key)
    assert s["gmp_connected"] is True
    assert s["linked_arrays"] == 0
    assert s["unlinked_accounts"] == 1
    assert s["complete"] is True          # banner hides
    assert s["next_step"] == "link_accounts"  # softer nudge remains


def test_gmp_connected_and_linked_is_done(client):
    """GMP connected AND linked to an array → complete + next_step 'done'."""
    tid, key = _make_tenant()
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Array A", fuel_type="solar")
        db.add(arr)
        db.flush()
        db.add(UtilityAccount(
            tenant_id=tid, provider="gmp",
            account_number="gmp_" + secrets.token_hex(3), array_id=arr.id,
        ))
        db.commit()

    s = _status(client, key)
    assert s["gmp_connected"] is True
    assert s["linked_arrays"] == 1
    assert s["complete"] is True
    assert s["next_step"] == "done"
