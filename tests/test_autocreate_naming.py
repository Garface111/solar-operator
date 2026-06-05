"""
Tests for client display-name priority during auto-create + placeholder adoption.

Priority order (highest to lowest):
  1. customer_name on normalized account (or extra.customerName) — currently dead code
     for GMP and VEC; neither adapter sets this field in the normalized output.
  2. user email
  3. user username

Also verifies that per-array nicknames (the GMP/VEC "name for this meter")
are NOT used as client names — they become the Array name instead.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array


def _tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Naming Test Co", contact_email="op@naming.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})


# ── (1) Email beats username when both are present ────────────────────────────

def test_email_beats_username_when_both_present(client):
    """When both email and username are captured, email is used as the client name."""
    tid, key = _tenant()

    payload = {
        "provider": "gmp",
        "user": {"email": "priority@gmp.test", "fullName": "P User", "username": "pri_username"},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": [{
            "accountNumber": "PRI001",
            "nickname": "Priority Array",
            "customerNumber": "cust_pri",
            "isPrimary": True,
            "solarNetMeter": True,
        }],
    }
    resp = _sync(client, key, payload)
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
        assert c.name == "priority@gmp.test", c.name
        assert c.gmp_email == "priority@gmp.test"
        assert c.gmp_username == "pri_username"
        assert c.gmp_autopopulate is True


# ── (2) VEC customerName does NOT become the client display name ──────────────
# The VEC adapter maps raw `customerName` → normalized `nickname`
# (used for the Array name), NOT to `customer_name` or `extra.customerName`
# (what the auto-create naming code checks). The client name therefore falls
# back to the captured user email.
#
# NOTE: this behaviour is a naming gap — the code comment says "customer_name
# (e.g. VEC)" is the top-priority label, but VEC never populates that field.

# ── (2) VEC: customerName IS the right client-level label ───────────────────
# When the operator's name is "Bob's Electric LLC" but their login is
# bob@example.com, the customerName from VEC is the human-friendly company
# label and beats the email/username. The adapter populates both
# customer_name (client-level) and nickname (array-level fallback).

def test_vec_customer_name_used_as_client_name(client):
    tid, key = _tenant()

    payload = {
        "provider": "vec",
        "user": {"email": "owner@farm.test", "fullName": "Farm Owner"},
        "auth": {},
        "accounts": [{"accountNumber": "V900", "customerName": "West Glover Farm LLC"}],
        "bills": [],
        "usage": [],
    }
    resp = _sync(client, key, payload)
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
        # Client name is the VEC customerName — it's the company label, the
        # most useful display string for the operator.
        assert c.name == "West Glover Farm LLC", (
            f"expected VEC customerName as client name, got {c.name!r}"
        )
        # Same value flows to the Array name as a fallback nickname.
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(arrays) == 1
        assert arrays[0].name == "West Glover Farm LLC"


# ── (3) Display name capped at 200 chars ─────────────────────────────────────

def test_display_name_truncated_to_200_chars(client):
    """A 201-char username is stored as exactly 200 chars."""
    tid, key = _tenant()

    long_username = "u" * 201

    payload = {
        "provider": "gmp",
        "user": {"email": "", "fullName": "U", "username": long_username},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": [{
            "accountNumber": "LONG001",
            "nickname": None,
            "customerNumber": "cust_long",
            "isPrimary": True,
            "solarNetMeter": True,
        }],
    }
    resp = _sync(client, key, payload)
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
    assert len(c.name) == 200
    assert c.name == long_username[:200]
