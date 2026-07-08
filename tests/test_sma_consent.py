"""SMA owner-consent connect flow.

SMA's model: our ONE registered app (env creds) + per-owner backchannel
consent — the owner approves inside Sunny Portal; no passwords or keys ever
touch us. These tests pin the whole lifecycle with mocked httpx: availability
gating, consent request persistence, status polling, and the attach cascade
storing ONLY {system_id} per connection (app creds stay in the environment).

The mocked httpx shapes MIRROR the shapes VERIFIED against the live SMA sandbox
on 2026-07-08 (see the banner in api/inverters/sma.py):
  • POST bc-authorize (Bearer + JSON {"loginHint"}) → 201 {"state": "Pending"|
    "Accepted"|"Revoked", ...}. Re-POSTing reads the current state (no GET).
  • GET /plants → {"plants": [{"plantId","name","timezone"}]}.
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


def _wire_sma(monkeypatch, *, plants=None, consent="Pending"):
    """Mock the SMA app env + every httpx call the adapter makes, using the
    VERIFIED sandbox shapes. `consent` is SMA's capitalized enum (Pending|
    Accepted|Revoked); bc-authorize returns it on every POST."""
    import api.inverters.sma as sma
    cid = "app-" + secrets.token_hex(4)          # unique per test — token cache is module-level
    monkeypatch.setenv("SMA_APP_CLIENT_ID", cid)
    monkeypatch.setenv("SMA_APP_CLIENT_SECRET", "app-secret")

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        if url == sma.AUTH_URL:
            return _FakeResp(200, {"access_token": "tok", "expires_in": 3600})
        if url.endswith("/oauth2/v2/bc-authorize"):
            # Verified: Bearer + JSON {"loginHint": ...} → 201 {state, ...}
            assert json and "loginHint" in json, "bc-authorize must send JSON loginHint"
            return _FakeResp(201, {"loginHint": json["loginHint"], "state": consent,
                                   "expirationDate": "2026-07-15T00:00:00Z",
                                   "interval": 1800})
        return _FakeResp(404, {"error": f"unexpected POST {url}"})

    def fake_get(url, headers=None, params=None, timeout=None):
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
    _wire_sma(monkeypatch, consent="Pending")
    r = client.post("/v1/array-owners/sma/consent", headers=_auth(tid),
                    json={"owner_email": "Owner@Farm.com"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    with SessionLocal() as db:
        row = db.execute(select(SmaConsent).where(
            SmaConsent.tenant_id == tid)).scalar_one()
        assert row.owner_email_lc == "owner@farm.com"
        assert row.status == "pending"

    # Poll while pending, then after the owner approves (status re-POSTs
    # bc-authorize; SMA's capitalized state normalizes to lowercase).
    r = client.get("/v1/array-owners/sma/consent/status",
                   params={"owner_email": "owner@farm.com"}, headers=_auth(tid))
    assert r.json()["status"] == "pending"
    _wire_sma(monkeypatch, consent="Accepted")
    r = client.get("/v1/array-owners/sma/consent/status",
                   params={"owner_email": "owner@farm.com"}, headers=_auth(tid))
    assert r.json()["status"] == "accepted"
    # Unknown email → 404, never a silent empty state.
    r = client.get("/v1/array-owners/sma/consent/status",
                   params={"owner_email": "nobody@x.com"}, headers=_auth(tid))
    assert r.status_code == 404


def test_consent_already_accepted_short_circuits(client, monkeypatch):
    """A returning owner who already approved comes back "accepted" on the very
    first bc-authorize POST — the UI can skip the waiting screen."""
    tid = _make_tenant()
    _wire_sma(monkeypatch, consent="Accepted")
    r = client.post("/v1/array-owners/sma/consent", headers=_auth(tid),
                    json={"owner_email": "back@again.com"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    assert "Already connected" in r.json()["message"]


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


# ── Adapter-level shape tests (verified against the sandbox 2026-07-08) ─────────

def test_pv_generation_parses_verified_set_envelope():
    """VERIFIED envelope: {plant, setType, resolution, set:[{time, pvGeneration}]}.
    We take the last non-null pvGeneration in the set."""
    import api.inverters.sma as sma
    body = {
        "plant": {"plantId": "13"},
        "setType": "EnergyAndPowerPv",
        "resolution": "OneDay",
        "set": [
            {"time": "2026-07-01T00:00:00", "pvGeneration": 12000.0},
            {"time": "2026-07-01T00:00:00", "pvGeneration": 15250.5},
        ],
    }
    val, ts = sma._pv_generation(body)
    assert val == 15250.5
    assert ts == "2026-07-01T00:00:00"
    # Empty set (sandbox test plants) → no value, never a crash.
    assert sma._pv_generation({"set": []}) == (None, None)
    # Legacy/top-level fallbacks still tolerated.
    assert sma._pv_generation({"pvGeneration": 42.0}) == (42.0, None)
    assert sma._pv_generation({"pvGeneration": {"value": 9.0, "time": "t"}}) == (9.0, "t")


def test_request_consent_sends_bearer_json_loginhint(monkeypatch):
    """bc-authorize must POST JSON {"loginHint": ...} with a Bearer token — the
    shape the sandbox accepted (form + client-creds-in-body were 401/415)."""
    import api.inverters.sma as sma
    monkeypatch.setenv("SMA_APP_CLIENT_ID", "app-x")
    monkeypatch.setenv("SMA_APP_CLIENT_SECRET", "app-y")
    captured = {}

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        if url == sma.AUTH_URL:
            return _FakeResp(200, {"access_token": "TOK", "expires_in": 3600})
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers or {}
        return _FakeResp(201, {"loginHint": json["loginHint"], "state": "Pending"})

    monkeypatch.setattr(sma.httpx, "post", fake_post)
    out = sma.request_consent("owner@farm.com")
    assert out["state"] == "pending"
    assert captured["url"].endswith("/oauth2/v2/bc-authorize")
    assert captured["json"] == {"loginHint": "owner@farm.com", "scope": "monitoringApi:read"}
    assert captured["headers"].get("Authorization") == "Bearer TOK"


def test_discover_systems_parses_plantid_shape(monkeypatch):
    """VERIFIED /plants shape: {"plants":[{plantId,name,timezone}]}."""
    import api.inverters.sma as sma
    monkeypatch.setenv("SMA_APP_CLIENT_ID", "app-x")
    monkeypatch.setenv("SMA_APP_CLIENT_SECRET", "app-y")

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        return _FakeResp(200, {"access_token": "TOK", "expires_in": 3600})

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp(200, {"plants": [
            {"plantId": "13", "name": "Testplant 1", "timezone": "Europe/Berlin"},
            {"plantId": "24", "name": "", "timezone": "Europe/Berlin"},
        ]})

    monkeypatch.setattr(sma.httpx, "post", fake_post)
    monkeypatch.setattr(sma.httpx, "get", fake_get)
    out = sma.discover_systems()
    assert {s["system_id"] for s in out} == {"13", "24"}
    # Blank name falls back to a stable label, never empty.
    assert any(s["name"] == "SMA plant 24" for s in out)
