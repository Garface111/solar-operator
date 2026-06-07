"""GET /v1/demo/enter — the shared-demo magic-link entry point."""
from __future__ import annotations

from datetime import date

from api.account import tenant_from_session, DEMO_TENANT_ID
from scripts.seed_demo_tenant import seed

FIXED_TODAY = date(2026, 6, 6)


def test_demo_enter_returns_token_for_demo_tenant(client):
    seed(today=FIXED_TODAY)
    resp = client.get("/v1/demo/enter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["is_demo"] is True
    assert body["redirect"] == "/accounts/"
    assert body["session_token"]

    # The minted token resolves to the demo tenant.
    t = tenant_from_session(f"Bearer {body['session_token']}")
    assert t.id == DEMO_TENANT_ID
    assert t.is_demo is True


def test_demo_enter_token_usable_for_get_but_not_write(client):
    seed(today=FIXED_TODAY)
    token = client.get("/v1/demo/enter").json()["session_token"]
    auth = {"Authorization": f"Bearer {token}"}

    me = client.get("/v1/account", headers=auth)
    assert me.status_code == 200
    assert me.json()["is_demo"] is True

    blocked = client.post(
        "/v1/account/clients", json={"name": "Nope"}, headers=auth
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "demo-read-only"


def test_demo_enter_503_when_not_seeded(client):
    """If the demo tenant was wiped, /v1/demo/enter degrades gracefully."""
    from api.db import SessionLocal
    from api.models import Tenant
    from scripts.seed_demo_tenant import _wipe_demo_data
    with SessionLocal() as db:
        t = db.get(Tenant, DEMO_TENANT_ID)
        if t is not None:
            _wipe_demo_data(db)
            db.delete(t)
            db.commit()
    resp = client.get("/v1/demo/enter")
    assert resp.status_code == 503
