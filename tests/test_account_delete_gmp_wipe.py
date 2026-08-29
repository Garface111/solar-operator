"""Deleting an account must clear the GMP identifiers on its Client rows.

Regression: /v1/account/delete used to assign tenant.gmp_email/gmp_username,
but Tenant has no such columns — SQLAlchemy took them as plain instance
attributes and wrote nothing, so the GMP login email/username survived the
"delete" on the tenant's clients. That is a live matching key, not just stale
PII: /v1/sync appends captured arrays to a client by matching an incoming GMP
capture against clients.gmp_email / gmp_username, so a deleted account could
still have fresh utility data attached to it.

Deliberately non-Bruce data (repo rule: don't overfit tests to one customer).
"""
from __future__ import annotations

import secrets

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Client, Tenant


def _make_tenant_with_gmp_client() -> tuple[str, int, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Wolf Creek Solar",
            contact_email=f"{tid}@example.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard",
            subscription_status="active",
            active=True,
            is_demo=False,
        ))
        c = Client(
            tenant_id=tid,
            name="Wolf Creek Community Array",
            contact_email="offtaker@example.com",
            gmp_email="wolfcreek.ops@example.com",
            gmp_username="wolfcreek_ops",
            gmp_autopopulate=True,
            active=True,
        )
        db.add(c)
        db.commit()
        cid = c.id
    return tid, cid, f"Bearer {mint_session_for_tenant(tid)}"


def test_delete_clears_client_gmp_identifiers(client):
    tid, cid, auth = _make_tenant_with_gmp_client()

    r = client.post("/v1/account/delete", json={"confirm": "DELETE"},
                    headers={"Authorization": auth})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.gmp_email is None, "GMP email survived the account delete"
        assert c.gmp_username is None, "GMP username survived the account delete"
        assert c.gmp_autopopulate is False, "capture can still auto-attach arrays"

        t = db.get(Tenant, tid)
        assert t.active is False
        assert t.subscription_status == "deleted"
        assert t.contact_email.startswith("deleted+")
        assert t.contact_email.endswith("@invalid.local")


def test_delete_requires_exact_confirm_string(client):
    _tid, cid, auth = _make_tenant_with_gmp_client()

    r = client.post("/v1/account/delete", json={"confirm": "delete"},
                    headers={"Authorization": auth})
    assert r.status_code == 400

    # A refused delete must not have touched the client's GMP identifiers.
    with SessionLocal() as db:
        assert db.get(Client, cid).gmp_email == "wolfcreek.ops@example.com"
