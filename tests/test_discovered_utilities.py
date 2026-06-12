"""Fleet-learning tests: discovered SmartHub utilities.

Covers the v1.6.2 discovery loop:
  - derive_provider_from_host: curated vs discovered vs non-smarthub
  - parse_extension_payload: hostname is authoritative, no more VEC masquerade
  - get_adapter routes sh_* codes to the smarthub adapter
  - /v1/sync mints a DiscoveredUtility row + capture event for unknown hosts
  - /v1/extension/scrape-miss records parser misses
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from api.adapters import get_adapter, smarthub
from api.adapters.smarthub import derive_provider_from_host, parse_extension_payload
from api.db import SessionLocal
from api.models import DiscoveredUtility, Tenant, UtilityAccount


def _make_tenant() -> tuple[str, str]:
    import secrets
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Disco Test", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


# ─── derive_provider_from_host ────────────────────────────────────────────────

def test_derive_curated_host():
    d = derive_provider_from_host("washingtonelectric.smarthub.coop")
    assert d == {
        "provider": "wec",
        "name": d["name"],  # label comes from the CSV catalog
        "host": "washingtonelectric.smarthub.coop",
        "discovered": False,
    }
    assert "Washington" in d["name"]


def test_derive_unknown_host_mints_deterministic_code():
    d = derive_provider_from_host("Norwich-Solar.smarthub.coop")
    assert d["provider"] == "sh_norwich_solar"
    assert d["discovered"] is True
    assert d["host"] == "norwich-solar.smarthub.coop"
    # Deterministic: same host, same code
    assert derive_provider_from_host("norwich-solar.smarthub.coop")["provider"] == "sh_norwich_solar"


def test_derive_non_smarthub_host_is_none():
    assert derive_provider_from_host("greenmountainpower.com") is None
    assert derive_provider_from_host("") is None


def test_derived_code_fits_db_column():
    long_sub = "a" * 80
    d = derive_provider_from_host(f"{long_sub}.smarthub.coop")
    assert len(d["provider"]) <= 40  # UtilityAccount.provider is VARCHAR(40)


# ─── parse_extension_payload: hostname authoritative ─────────────────────────

def _payload(provider: str, hostname: str) -> dict:
    return {
        "provider": provider,
        "user": {"hostname": hostname, "utility": "X"},
        "accounts": [{"accountNumber": "12345", "customerName": "JANE DOE"}],
        "bills": [],
        "usage": [],
    }


def test_unknown_host_no_longer_masquerades_as_vec():
    n = parse_extension_payload(_payload("vec", "brandnewcoop.smarthub.coop"))
    assert n["provider"] == "sh_brandnewcoop"
    assert n["smarthub_discovered"] is True
    assert n["smarthub_host"] == "brandnewcoop.smarthub.coop"


def test_curated_host_corrects_wrong_provider_claim():
    # Legacy extension claiming vec while actually on WEC → hostname wins
    n = parse_extension_payload(_payload("vec", "washingtonelectric.smarthub.coop"))
    assert n["provider"] == "wec"
    assert n["smarthub_discovered"] is False


def test_no_hostname_falls_back_to_vec():
    p = _payload("notacode", "")
    p["user"] = {}
    n = parse_extension_payload(p)
    assert n["provider"] == "vec"


def test_capture_method_telemetry_passthrough():
    p = _payload("wec", "washingtonelectric.smarthub.coop")
    p["captureMethod"] = "API"
    p["extensionVersion"] = "1.6.2"
    n = parse_extension_payload(p)
    assert n["capture_method"] == "api"
    assert n["extension_version"] == "1.6.2"


# ─── adapter routing ──────────────────────────────────────────────────────────

def test_get_adapter_routes_discovered_codes():
    assert get_adapter("sh_brandnewcoop") is smarthub
    with pytest.raises(ValueError):
        get_adapter("definitely_not_a_provider")


def test_is_smarthub_provider_accepts_discovered():
    assert smarthub.is_smarthub_provider("sh_anything") is True
    assert smarthub.is_smarthub_provider("wec") is True
    assert smarthub.is_smarthub_provider("gmp") is False


# ─── /v1/sync end-to-end discovery ────────────────────────────────────────────

def test_sync_from_unknown_host_mints_discovery_row(client):
    tid, key = _make_tenant()
    resp = client.post(
        "/v1/sync",
        json=_payload("vec", "freshcoop.smarthub.coop"),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        disc = db.execute(
            select(DiscoveredUtility).where(
                DiscoveredUtility.host == "freshcoop.smarthub.coop")
        ).scalar_one_or_none()
        assert disc is not None
        assert disc.provider_code == "sh_freshcoop"
        assert disc.capture_count == 1
        # Account landed under the discovered code, not vec
        acct = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
        ).scalars().all()
        assert len(acct) == 1
        assert acct[0].provider == "sh_freshcoop"

    # Second capture increments, doesn't duplicate
    resp = client.post(
        "/v1/sync",
        json=_payload("vec", "freshcoop.smarthub.coop"),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    with SessionLocal() as db:
        rows = db.execute(
            select(DiscoveredUtility).where(
                DiscoveredUtility.host == "freshcoop.smarthub.coop")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].capture_count == 2


def test_sync_from_curated_host_no_discovery_row(client):
    _tid, key = _make_tenant()
    resp = client.post(
        "/v1/sync",
        json=_payload("wec", "washingtonelectric.smarthub.coop"),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        disc = db.execute(
            select(DiscoveredUtility).where(
                DiscoveredUtility.host == "washingtonelectric.smarthub.coop")
        ).scalar_one_or_none()
        assert disc is None


# ─── /v1/extension/scrape-miss ────────────────────────────────────────────────

def test_scrape_miss_records_drift(client):
    _tid, key = _make_tenant()
    resp = client.post(
        "/v1/extension/scrape-miss",
        json={
            "hostname": "weirdlayout.smarthub.coop",
            "page": "/ui/#/billingHistory",
            "extensionVersion": "1.6.2",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with SessionLocal() as db:
        disc = db.execute(
            select(DiscoveredUtility).where(
                DiscoveredUtility.host == "weirdlayout.smarthub.coop")
        ).scalar_one_or_none()
        assert disc is not None
        assert disc.last_capture_method == "miss"
        assert disc.last_extension_version == "1.6.2"


def test_scrape_miss_rejects_non_smarthub(client):
    _tid, key = _make_tenant()
    resp = client.post(
        "/v1/extension/scrape-miss",
        json={"hostname": "evil.example.com"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
