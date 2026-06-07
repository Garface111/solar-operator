"""Operator-name / company-name split: two independent fields.

The legacy single `name` column did double duty as both the operator's
personal name and the company name. After the Jun-2026 split they're
distinct columns. POST /v1/account/name writes operator_name only.
POST /v1/account/company-name writes company_name AND mirrors to the
legacy `name` column for back-compat readers. No cross-contamination
in either direction.
"""
from __future__ import annotations

import secrets

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Legacy Co",
            contact_email=f"{tid}@example.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard",
            subscription_status="active",
            active=True,
            is_demo=False,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def test_operator_and_company_name_are_independent(client):
    tid, auth = _make_tenant()

    # 1. Write operator name. Should NOT touch company_name or legacy name.
    r = client.post("/v1/account/name", json={"name": "Alice"},
                    headers={"Authorization": auth})
    assert r.status_code == 200, r.text

    # 2. Write company name. Should set company_name AND mirror to legacy name.
    r = client.post("/v1/account/company-name", json={"name": "Acme Co"},
                    headers={"Authorization": auth})
    assert r.status_code == 200, r.text

    # 3. GET returns both, distinctly.
    r = client.get("/v1/account", headers={"Authorization": auth})
    assert r.status_code == 200
    body = r.json()
    assert body["operator_name"] == "Alice"
    assert body["company_name"] == "Acme Co"

    # 4. Legacy column was mirrored by the company write (not the operator write).
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.operator_name == "Alice"
        assert t.company_name == "Acme Co"
        # The operator write didn't touch `name`; the company write mirrored to it.
        assert t.name == "Acme Co"


def test_operator_name_write_does_not_clobber_company(client):
    """Updating the operator's personal name must not affect company fields."""
    tid, auth = _make_tenant()

    # Seed a company name first.
    client.post("/v1/account/company-name", json={"name": "Original Co"},
                headers={"Authorization": auth})

    # Now change ONLY the operator name.
    client.post("/v1/account/name", json={"name": "Bob"},
                headers={"Authorization": auth})

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.operator_name == "Bob"
        assert t.company_name == "Original Co"
        assert t.name == "Original Co"  # legacy mirror unchanged by operator write
