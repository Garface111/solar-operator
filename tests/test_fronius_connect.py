"""Account-level Fronius discovery / connect ("paste one key, attach all").

Mirrors tests/test_inverter_stress.py's SolarEdge coverage for the new
/v1/array-owners/fronius/{discover,connect-account} cascade: the Solar.web
Query API's /pvsystems lists every system an AccessKey can read (grounded
live 2026-07-04), so Fronius onboarding is now one-credential like SolarEdge.
pv_system_id is a STRING (UUID) throughout — that's the shape difference these
tests pin. All httpx is mocked — no real network.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, InverterConnection, Tenant


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
    key = "frcon_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Fronius Connect Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid


def _auth(tid: str) -> dict:
    from api.account import _sign_session
    return {"Authorization": f"Bearer {_sign_session(tid)}"}


def _system(sid: str, name: str, peak_wp: float = 20000.0) -> dict:
    return {
        "pvSystemId": sid, "name": name, "peakPower": peak_wp,
        "address": {"street": "1 Sun Rd", "city": "Montpelier", "country": "USA"},
    }


def _pvsystems_get(systems: list[dict], *, status: int = 200):
    """Fake httpx.get serving GET /pvsystems paginated by offset/limit."""
    def fake_get(url, headers=None, params=None, timeout=None):
        params = params or {}
        if url.endswith("/pvsystems"):
            if status != 200:
                return _FakeResp(status, {"error": "forced"})
            off = int(params.get("offset", 0))
            lim = int(params.get("limit", 50))
            return _FakeResp(200, {"pvSystems": systems[off:off + lim]})
        return _FakeResp(404, {"error": f"unexpected url {url}"})
    return fake_get


_KEYS = {"access_key_id": "FKIATEST", "access_key_value": "secret-value"}


def _arrays_for(tid: str) -> list[Array]:
    with SessionLocal() as db:
        return list(db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all())


def test_discover_lists_and_paginates(client, monkeypatch):
    import api.inverters.fronius as fr
    systems = [_system(f"uuid-{i:03d}", f"Sys {i}") for i in range(120)]
    monkeypatch.setattr(fr.httpx, "get", _pvsystems_get(systems))
    tid = _make_tenant()
    r = client.post("/v1/array-owners/fronius/discover",
                    headers=_auth(tid), json=_KEYS)
    assert r.status_code == 200, r.text
    got = r.json()["systems"]
    assert len(got) == 120                        # paginated past the 50/page limit
    assert got[0]["pv_system_id"] == "uuid-000"   # STRING id, never int-coerced
    assert got[0]["peak_power_kw"] == 20.0        # Wp -> kW for display
    assert "Montpelier" in got[0]["address"]


def test_discover_bad_key_is_400_with_guidance(client, monkeypatch):
    import api.inverters.fronius as fr
    monkeypatch.setattr(fr.httpx, "get", _pvsystems_get([], status=401))
    tid = _make_tenant()
    r = client.post("/v1/array-owners/fronius/discover",
                    headers=_auth(tid), json=_KEYS)
    assert r.status_code == 400
    assert "AccessKey" in r.json()["detail"]


def test_connect_account_creates_matches_and_is_idempotent(client, monkeypatch):
    import api.inverters.fronius as fr
    systems = [_system("uuid-a", "Hilltop"), _system("uuid-b", "Barn Roof")]
    monkeypatch.setattr(fr.httpx, "get", _pvsystems_get(systems))
    tid = _make_tenant()
    # Pre-existing array named exactly like one system -> must ATTACH, not dup.
    with SessionLocal() as db:
        db.add(Array(tenant_id=tid, name="Hilltop", fuel_type="solar"))
        db.commit()

    r = client.post("/v1/array-owners/fronius/connect-account",
                    headers=_auth(tid), json=_KEYS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["connected"]) == 2
    assert len(body["matched"]) == 1              # Hilltop attached by name
    assert len(body["created"]) == 1              # Barn Roof created fresh

    arrays = _arrays_for(tid)
    assert {a.name for a in arrays} == {"Hilltop", "Barn Roof"}
    with SessionLocal() as db:
        conns = db.execute(select(InverterConnection).where(
            InverterConnection.array_id.in_([a.id for a in arrays]))).scalars().all()
        assert len(conns) == 2
        assert all(c.vendor == "fronius" for c in conns)
        assert {c.config["pv_system_id"] for c in conns} == {"uuid-a", "uuid-b"}
        # The key rides along so the nightly pull can authenticate.
        assert all(c.config["access_key_value"] == "secret-value" for c in conns)

    # Re-run: idempotent — same arrays, same connections, nothing duplicated.
    r2 = client.post("/v1/array-owners/fronius/connect-account",
                     headers=_auth(tid), json=_KEYS)
    assert r2.status_code == 200
    assert len(r2.json()["created"]) == 0
    assert len(_arrays_for(tid)) == 2


def test_connect_account_subset_by_string_ids(client, monkeypatch):
    import api.inverters.fronius as fr
    systems = [_system("uuid-x", "Only Me"), _system("uuid-y", "Not Me")]
    monkeypatch.setattr(fr.httpx, "get", _pvsystems_get(systems))
    tid = _make_tenant()
    r = client.post("/v1/array-owners/fronius/connect-account",
                    headers=_auth(tid),
                    json={**_KEYS, "pv_system_ids": ["uuid-x"]})
    assert r.status_code == 200
    assert [a.name for a in _arrays_for(tid)] == ["Only Me"]
