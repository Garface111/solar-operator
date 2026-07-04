"""SMA owner-consent connect flow (v1.9.113).

SMA's model: our ONE registered app (env creds) + per-owner backchannel
consent — the owner approves inside Sunny Portal; no passwords or keys ever
touch us. These tests pin the whole lifecycle with mocked httpx: availability
gating, consent request persistence, status polling, and the attach cascade
storing ONLY {system_id} per connection (app creds stay in the environment).
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, InverterConnection, SmaConsent, Tenant


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return str(self._body)

    def json(self) -> dict:
        return self._body


def _make_tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    key = "smacon_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="SMA Consent Test", contact_email=f"{key}@t.test",
                      tenant_key=key, plan="standard", active=True))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    from api.account import _sign_session
    return {"Authorization": f"Bearer {_sign_session(tid)}"}


def _wire_sma(monkeypatch, *, plants=None, consent="pending"):
    """Mock the SMA app env + every httpx call the adapter makes."""
    import api.inverters.sma as sma
    cid = "app-" + secrets.token_hex(4)          # unique per test — token cache is module-level
    monkeypatch.setenv("SMA_APP_CLIENT_ID", cid)
    monkeypatch.setenv("SMA_APP_CLIENT_SECRET", "app-secret")

    def fake_post(url, data=None, timeout=None):
        if url == sma.AUTH_URL:
            return _FakeResp(200, {"access_token": "tok", "expires_in": 3600})
        if url.endswith("/oauth2/v2/bc-authorize"):
            return _FakeResp(200, {"auth_req_id": "req-123"})
        return _FakeResp(404, {"error": f"unexpected POST {url}"})

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/bc-authorize/" in url and url.endswith("/status"):
            return _FakeResp(200, {"status": consent})
        if url.endswith("/plants"):
            params = params or {}
            off, lim = int(params.get("offset", 0)), int(params.get("limit", 50))
            return _FakeResp(200, {"plants": (plants or [])[off:off + lim]})
        return _FakeResp(404, {"error": f"unexpected GET {url}"})

    monkeypatch.setattr(sma.httpx, "post", fake_post)
    monkeypatch.setattr(sma.httpx, "get", fake_get)


def test_available_reflects_app_registration(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.delenv("SMA_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("SMA_APP_CLIENT_SECRET", raising=False)
    r = client.get("/v1/array-owners/sma/available", headers=_auth(tid))
    assert r.json()["configured"] is False
    monkeypatch.setenv("SMA_APP_CLIENT_ID", "x")
    monkeypatch.setenv("SMA_APP_CLIENT_SECRET", "y")
    r = client.get("/v1/array-owners/sma/available", headers=_auth(tid))
    assert r.json()["configured"] is True


def test_consent_unconfigured_is_503_and_saves_nothing(client, monkeypatch):
    tid = _make_tenant()
    monkeypatch.delenv("SMA_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("SMA_APP_CLIENT_SECRET", raising=False)
    r = client.post("/v1/array-owners/sma/consent", headers=_auth(tid),
                    json={"owner_email": "owner@x.com"})
    assert r.status_code == 503
    with SessionLocal() as db:
        assert db.execute(select(SmaConsent).where(
            SmaConsent.tenant_id == tid)).scalars().all() == []


def test_consent_lifecycle_pending_to_accepted(client, monkeypatch):
    tid = _make_tenant()
    _wire_sma(monkeypatch, consent="pending")
    r = client.post("/v1/array-owners/sma/consent", headers=_auth(tid),
                    json={"owner_email": "Owner@Farm.com"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    with SessionLocal() as db:
        row = db.execute(select(SmaConsent).where(
            SmaConsent.tenant_id == tid)).scalar_one()
        assert row.owner_email_lc == "owner@farm.com"
        assert row.auth_req_id == "req-123"

    # Poll while pending, then after the owner approves.
    r = client.get("/v1/array-owners/sma/consent/status",
                   params={"owner_email": "owner@farm.com"}, headers=_auth(tid))
    assert r.json()["status"] == "pending"
    _wire_sma(monkeypatch, consent="accepted")
    r = client.get("/v1/array-owners/sma/consent/status",
                   params={"owner_email": "owner@farm.com"}, headers=_auth(tid))
    assert r.json()["status"] == "accepted"
    # Unknown email → 404, never a silent empty state.
    r = client.get("/v1/array-owners/sma/consent/status",
                   params={"owner_email": "nobody@x.com"}, headers=_auth(tid))
    assert r.status_code == 404


def test_connect_account_attaches_with_env_creds_only(client, monkeypatch):
    tid = _make_tenant()
    plants = [{"plantId": "PL-1", "name": "Hill Farm"},
              {"plantId": "PL-2", "name": "Valley Barn"}]
    _wire_sma(monkeypatch, plants=plants)
    with SessionLocal() as db:                    # exact-name match must attach
        db.add(Array(tenant_id=tid, name="Hill Farm", fuel_type="solar"))
        db.commit()

    r = client.post("/v1/array-owners/sma/connect-account", headers=_auth(tid),
                    json={"system_ids": ["PL-1", "PL-2"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["matched"]) == 1 and len(body["created"]) == 1
    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(
            Array.tenant_id == tid, Array.deleted_at.is_(None))).scalars().all()
        assert {a.name for a in arrays} == {"Hill Farm", "Valley Barn"}
        conns = db.execute(select(InverterConnection).where(
            InverterConnection.array_id.in_([a.id for a in arrays]))).scalars().all()
        assert all(c.vendor == "sma" for c in conns)
        # ONLY the system id per row — the app secret never lands in tenant data.
        assert all(set(c.config.keys()) == {"system_id"} for c in conns)

    # Idempotent re-run + subset selection.
    r2 = client.post("/v1/array-owners/sma/connect-account", headers=_auth(tid),
                     json={"system_ids": ["PL-1"]})
    assert r2.status_code == 200
    assert r2.json()["created"] == []
