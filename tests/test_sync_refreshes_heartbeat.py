"""
/v1/sync must refresh Tenant.extension_heartbeat_at.

The dashboard's "Last seen" line and the "hasn't checked in for 48+ hours"
banner read Tenant.extension_heartbeat_at. Before this fix that field was
stamped ONLY by /v1/extension/heartbeat, which background.js pings only while a
GMP tab is open — so an extension capturing all day via background portal
rotation (util-live, recaptures, VEC/other portals) reached /v1/sync without
ever refreshing "last seen", and the dashboard falsely showed the extension as
stale / disconnected while data was actively flowing.

Any authenticated /v1/sync is proof the extension is alive and paired, so it
now refreshes the heartbeat — even when the captured payload matches no client.

No network: /v1/sync only touches the DB on this path.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client


def _make_tenant(*, heartbeat_at: datetime | None = None) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Heartbeat Test Co", contact_email="op@hb.test",
            tenant_key=key, plan="standard", active=True,
            extension_heartbeat_at=heartbeat_at,
        ))
        db.commit()
    return tid, key


def _payload(email: str, account_number: str) -> dict:
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": "Captured User", "username": email},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": [{
            "accountNumber": account_number,
            "nickname": "Roof",
            "customerNumber": "cust_" + account_number,
            "serviceAddress": {"line1": account_number + " Main St", "city": "Chester"},
            "isPrimary": True,
            "solarNetMeter": True,
        }],
    }


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload,
                       headers={"Authorization": f"Bearer {key}"})


def test_sync_sets_heartbeat_when_previously_never_seen(client):
    """A tenant that has never heartbeat (None) gets stamped on first sync."""
    tid, key = _make_tenant(heartbeat_at=None)
    resp = _sync(client, key, _payload("who@gmp.test", "9001"))
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.extension_heartbeat_at is not None
        # freshly stamped — within the last minute
        assert datetime.utcnow() - t.extension_heartbeat_at < timedelta(minutes=1)


def test_sync_moves_stale_heartbeat_forward(client):
    """A 3-day-stale heartbeat (the exact failure: 'not seen in 48h' while
    captures flow) is moved forward by a sync."""
    stale = datetime.utcnow() - timedelta(days=3)
    tid, key = _make_tenant(heartbeat_at=stale)
    resp = _sync(client, key, _payload("who@gmp.test", "9002"))
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.extension_heartbeat_at > stale
        assert datetime.utcnow() - t.extension_heartbeat_at < timedelta(minutes=1)


def test_sync_refreshes_heartbeat_even_with_no_matching_client(client):
    """Reaching /v1/sync at all proves the extension is alive — the heartbeat
    refreshes regardless of whether the payload matches any existing client."""
    stale = datetime.utcnow() - timedelta(days=5)
    tid, key = _make_tenant(heartbeat_at=stale)
    # No Client exists on this tenant that matches the login.
    with SessionLocal() as db:
        assert db.execute(
            select(Client).where(Client.tenant_id == tid)
        ).scalars().all() == []
    resp = _sync(client, key, _payload("stranger@gmp.test", "9003"))
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert datetime.utcnow() - t.extension_heartbeat_at < timedelta(minutes=1)
