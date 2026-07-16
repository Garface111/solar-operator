"""Cloud Capture eager client-create (Ford 2026-07-16).

When a NEPOOL operator connects a utility login via Cloud Capture (onboarding
fork or the Master-account vault), a Client should appear on the Clients page
IMMEDIATELY — a "Pulling bills…" card — instead of the page staying empty until
the harvester's first capture lands. The eagerly-created client carries the same
login columns + autopop flag the /v1/sync matcher keys on, so the harvested bills
ATTACH to it (no duplicate) and its login-derived name upgrades to the real
portal holder name on that first capture.
"""
from __future__ import annotations

import secrets
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant


@pytest.fixture(autouse=True)
def _armed_collection(monkeypatch):
    """Arm Cloud Capture collection + fake encryption, and stub the credential
    upsert so these tests don't need a real Fernet key or DB cred row — we're
    asserting the CLIENT side-effect, not the credential storage."""
    monkeypatch.setenv("CLOUD_CAPTURE_COLLECT", "1")
    monkeypatch.setenv("CLOUD_CAPTURE_ENABLED", "1")
    monkeypatch.setattr("api.cloud_capture.cc.crypto_ready", lambda: True)
    monkeypatch.setattr(
        "api.cloud_capture.cc.upsert_credential",
        lambda db, tid, provider, username, password, login_host=None, enable=True: (
            SimpleNamespace(provider=provider, username=username)
        ),
    )


def _make_tenant(product: str | None = None) -> tuple[str, str, dict]:
    """Fresh tenant. Returns (tenant_id, tenant_key, session-auth-headers)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Eager Co", contact_email=f"{tid}@ex.test",
            tenant_key=key, plan="standard", active=True,
            **({"product": product} if product else {}),
        ))
        db.commit()
    return tid, key, {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def _save_login(client, auth, provider, username, *, login_host=None):
    body = {"provider": provider, "username": username,
            "password": "pw-" + secrets.token_hex(4), "consent": True, "enable": True}
    if login_host:
        body["login_host"] = login_host
    return client.post("/v1/cloud-capture/credentials",
                       headers={**auth, "Content-Type": "application/json"}, json=body)


def _clients(tid: str) -> list[Client]:
    with SessionLocal() as db:
        return db.execute(
            select(Client).where(Client.tenant_id == tid, Client.deleted_at.is_(None))
        ).scalars().all()


def test_gmp_login_eagerly_creates_pending_client(client):
    tid, _key, auth = _make_tenant()
    r = _save_login(client, auth, "gmp", "Owner.Person@example.com")
    assert r.status_code == 200, r.text

    rows = _clients(tid)
    assert len(rows) == 1
    c = rows[0]
    assert c.capture_pending is True
    assert c.gmp_autopopulate is True
    # login stored on the GMP columns the /v1/sync matcher keys on (lowercased)
    assert c.gmp_email == "owner.person@example.com"
    assert c.gmp_username == "Owner.Person@example.com"
    assert c.contact_email == "owner.person@example.com"
    # login-derived placeholder name (upgraded on first capture)
    assert c.name == "Owner Person"


def test_smarthub_login_eagerly_creates_pending_client(client):
    tid, _key, auth = _make_tenant()
    r = _save_login(client, auth, "vec", "farmer1",
                    login_host="vermontelectric.smarthub.coop")
    assert r.status_code == 200, r.text

    rows = _clients(tid)
    assert len(rows) == 1
    c = rows[0]
    assert c.capture_pending is True
    assert c.vec_autopopulate is True
    assert c.vec_username == "farmer1"
    assert c.vec_email is None  # not an email → username column only
    assert c.name == "Farmer1"


def test_duplicate_login_creates_no_duplicate_client(client):
    tid, _key, auth = _make_tenant()
    assert _save_login(client, auth, "gmp", "dup@example.com").status_code == 200
    assert _save_login(client, auth, "gmp", "DUP@example.com").status_code == 200  # case-fold
    assert len(_clients(tid)) == 1


def test_first_login_adopts_the_onboarding_placeholder(client):
    """Onboarding seeds a blank 'Your first client' placeholder at activation.
    The first cloud login should ADOPT it (not leave a stray) — so two logins
    yield exactly two client cards, the first reusing the placeholder."""
    tid, _key, auth = _make_tenant()
    with SessionLocal() as db:
        db.add(Client(tenant_id=tid, name="Your first client", active=True,
                      is_placeholder=True, gmp_autopopulate=True, vec_autopopulate=True))
        db.commit()

    assert _save_login(client, auth, "gmp", "first@example.com").status_code == 200
    rows = _clients(tid)
    assert len(rows) == 1  # adopted, not added
    c = rows[0]
    assert c.is_placeholder is False
    assert c.capture_pending is True
    assert c.gmp_email == "first@example.com"
    assert c.name == "First"  # renamed off the placeholder label

    assert _save_login(client, auth, "vec", "second",
                       login_host="vermontelectric.smarthub.coop").status_code == 200
    assert len(_clients(tid)) == 2  # second login makes a new card


def test_array_operator_tenant_creates_no_client(client):
    """AO uses offtakers, not the Client (sub-client) table — never mirror."""
    tid, _key, auth = _make_tenant(product="array_operator")
    r = _save_login(client, auth, "gmp", "owner@example.com")
    assert r.status_code == 200, r.text
    assert _clients(tid) == []


def test_inverter_cloud_creates_no_client(client):
    """Fronius/SMA/Chint are inverter telemetry, not utility bills — no autopop
    config, so no eager client (a pre-created card would never fill in)."""
    tid, _key, auth = _make_tenant()
    r = _save_login(client, auth, "fronius", "solar@example.com")
    assert r.status_code == 200, r.text
    assert _clients(tid) == []


def test_capture_clears_pending_and_upgrades_name(client):
    """The harvested bills land on the pre-created client (no dup) and flip it out
    of the pending state, upgrading the login-derived name to the portal holder."""
    tid, key, auth = _make_tenant()
    assert _save_login(client, auth, "gmp", "grower@example.com").status_code == 200
    (pending,) = _clients(tid)
    assert pending.capture_pending is True

    payload = {
        "provider": "gmp",
        "user": {"email": "grower@example.com", "fullName": "Green Acres Farm",
                 "username": "grower@example.com"},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": [{
            "accountNumber": "7001", "nickname": "South Field",
            "customerNumber": "cust_7001",
            "serviceAddress": {"line1": "7001 Main St", "city": "Chester"},
            "isPrimary": True, "solarNetMeter": True,
        }],
    }
    r = client.post("/v1/sync", json=payload,
                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text

    rows = _clients(tid)
    assert len(rows) == 1  # attached to the SAME client, no duplicate
    c = rows[0]
    assert c.id == pending.id
    assert c.capture_pending is False
    assert c.name == "Green Acres Farm"  # upgraded from "Grower"
    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
    assert len(arrays) == 1
    assert arrays[0].client_id == c.id
