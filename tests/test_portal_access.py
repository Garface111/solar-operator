"""Portal access roster (v1.9.112 multi-login vault).

The extension heartbeat may carry a vault report — WHICH utility portal logins
are saved client-side (usernames + health only, never passwords). The backend
persists it to portal_login_status and the dashboard reads GET /v1/portal-access
to show, per client: automated / failing / login still to collect.

Covered:
  1. heartbeat with a vault body persists rows; body-less heartbeat still 200s
  2. roster matches a saved login to the client claiming that gmp_email
  3. client with no portal identity → status "no_portal_identity"
  4. saved login no client claims → unassigned_logins
  5. paused/failing login → status "failing"; recent last_ok_at → "automated"
  6. re-reported snapshot REPLACES per provider (removed login disappears)
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, PortalLoginStatus, now


def _mk_tenant(**over) -> tuple[str, str, str]:
    """Fresh tenant. Returns (tenant_id, tenant_key, session_bearer)."""
    from api.account import mint_session_for_tenant
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    fields = dict(id=tid, name="Portal Test Op", contact_email=f"{key}@op.test",
                  tenant_key=key, plan="standard", active=True)
    fields.update(over)
    with SessionLocal() as db:
        db.add(Tenant(**fields))
        db.commit()
    return tid, key, mint_session_for_tenant(tid)


def _add_client(tid: str, name: str, **over) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name=name, **over)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def _hb(client, key: str, vault=None):
    kwargs = {"headers": {"Authorization": f"Bearer {key}"}}
    if vault is not None:
        kwargs["json"] = {"vault": vault}
    return client.post("/v1/extension/heartbeat", **kwargs)


def test_heartbeat_persists_vault_report_and_bodyless_still_works(client):
    tid, key, _sess = _mk_tenant()
    r = _hb(client, key)                       # body-less: the 59-of-60 case
    assert r.status_code == 200
    r = _hb(client, key, vault=[
        {"code": "gmp", "username": "Bruce@GMCS.com", "enabled": True,
         "last_ok_at": now().isoformat(), "fails": 0, "paused": False},
        {"code": "gmp", "username": "client2@x.com", "enabled": True,
         "last_ok_at": None, "fails": 0, "paused": False},
    ])
    assert r.status_code == 200
    with SessionLocal() as db:
        rows = db.execute(select(PortalLoginStatus).where(
            PortalLoginStatus.tenant_id == tid)).scalars().all()
        assert {(x.provider, x.username_lc) for x in rows} == {
            ("gmp", "bruce@gmcs.com"), ("gmp", "client2@x.com")}
        # Display casing preserved; passwords obviously nowhere.
        assert {x.username for x in rows} == {"Bruce@GMCS.com", "client2@x.com"}
    # A later body-less heartbeat must not disturb the stored rows.
    assert _hb(client, key).status_code == 200
    with SessionLocal() as db:
        assert db.execute(select(PortalLoginStatus).where(
            PortalLoginStatus.tenant_id == tid)).scalars().all()


def test_roster_matches_clients_and_flags_gaps(client):
    tid, key, sess = _mk_tenant()
    _add_client(tid, "Automated LLC", gmp_email="auto@x.com")
    _add_client(tid, "Failing Farm", gmp_email="fail@x.com")
    _add_client(tid, "No Login Yet", gmp_email="missing@x.com")
    _add_client(tid, "No Identity Co")        # operator never said which login
    _hb(client, key, vault=[
        {"code": "gmp", "username": "auto@x.com", "enabled": True,
         "last_ok_at": now().isoformat(), "fails": 0, "paused": False},
        {"code": "gmp", "username": "fail@x.com", "enabled": True,
         "last_ok_at": (now() - timedelta(days=9)).isoformat(), "fails": 3, "paused": True},
        {"code": "gmp", "username": "orphan@x.com", "enabled": True,
         "last_ok_at": None, "fails": 0, "paused": False},
    ])
    r = client.get("/v1/portal-access", headers={"Authorization": f"Bearer {sess}"})
    assert r.status_code == 200
    body = r.json()
    by_client = {row["client"]: row for row in body["clients"]}
    assert by_client["Automated LLC"]["status"] == "automated"
    assert by_client["Failing Farm"]["status"] == "failing"
    assert by_client["No Login Yet"]["status"] == "login_missing"
    assert by_client["No Identity Co"]["status"] == "no_portal_identity"
    assert [u["username"] for u in body["unassigned_logins"]] == ["orphan@x.com"]
    # extension_alive: the heartbeat above just landed.
    assert body["extension_alive"] is True


def test_snapshot_replace_per_provider(client):
    tid, key, sess = _mk_tenant()
    _hb(client, key, vault=[
        {"code": "gmp", "username": "a@x.com", "enabled": True, "fails": 0},
        {"code": "gmp", "username": "b@x.com", "enabled": True, "fails": 0},
        {"code": "vec", "username": "coop@x.com", "enabled": True, "fails": 0},
    ])
    # Operator removed b@x.com in the popup → next snapshot omits it. vec is
    # NOT in this report (unchanged on that machine? no — report is full-vault;
    # here we simulate a gmp-only report) and must be untouched.
    _hb(client, key, vault=[
        {"code": "gmp", "username": "a@x.com", "enabled": True, "fails": 0},
    ])
    with SessionLocal() as db:
        rows = db.execute(select(PortalLoginStatus).where(
            PortalLoginStatus.tenant_id == tid)).scalars().all()
        assert {(x.provider, x.username_lc) for x in rows} == {
            ("gmp", "a@x.com"), ("vec", "coop@x.com")}


def test_saved_but_never_pulled_is_saved_pending(client):
    tid, key, sess = _mk_tenant()
    _add_client(tid, "Fresh Save Inc", gmp_username="freshsave")
    _hb(client, key, vault=[
        {"code": "gmp", "username": "freshsave", "enabled": True,
         "last_ok_at": None, "fails": 0, "paused": False},
    ])
    r = client.get("/v1/portal-access", headers={"Authorization": f"Bearer {sess}"})
    row = [x for x in r.json()["clients"] if x["client"] == "Fresh Save Inc"][0]
    assert row["status"] == "saved_pending"
    assert row["login_username"] == "freshsave"


def test_login_crossing_into_failing_alerts_ford_once(client, monkeypatch):
    """A login going healthy -> failing must alert immediately (not wait for
    Monday's aggregate scorecard) -- but only on the TRANSITION, so a login
    stuck failing for weeks doesn't re-alert every heartbeat (Ford, 2026-07-08:
    "find every instance of us intentionally sabotaging our own reliability")."""
    sent = []
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda subject, body: sent.append((subject, body)))
    tid, key, _sess = _mk_tenant()
    # Healthy first.
    _hb(client, key, vault=[
        {"code": "gmp", "username": "watched@x.com", "enabled": True,
         "last_ok_at": now().isoformat(), "fails": 0, "paused": False},
    ])
    assert sent == []
    # Crosses into failing.
    _hb(client, key, vault=[
        {"code": "gmp", "username": "watched@x.com", "enabled": True,
         "last_ok_at": now().isoformat(), "fails": 3, "paused": True},
    ])
    assert len(sent) == 1
    assert "gmp" in sent[0][0]
    # Still failing on the next heartbeat -- no repeat alert.
    _hb(client, key, vault=[
        {"code": "gmp", "username": "watched@x.com", "enabled": True,
         "last_ok_at": now().isoformat(), "fails": 4, "paused": True},
    ])
    assert len(sent) == 1
