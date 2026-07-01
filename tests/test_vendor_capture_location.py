"""Vendor-capture LOCATION wiring (Ford, 2026-07-01) — the extension (v1.9.103)
now deep-scans Chint/Fronius/SMA portal payloads for a site's lat/lng or address
and sends it as CaptureSite.{latitude,longitude,address}. These tests prove the
BACKEND half of that pipeline: when the extension sends location, the Array
actually gets geocoded — independent of whether any given live portal's JSON
happens to contain location data (that's a client-side scraping question this
suite can't answer; see array_owners._set_array_location).

Never overwrites a manual location or a utility-address geocode (fills only
when the array has no location yet) — the "additive, never regress" contract.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, Tenant

CAPTURE = "/v1/array-owners/inverter-capture"


def _mk_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="LocTest", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
            product="array_operator",
        ))
        db.commit()
    return tid, key


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _array_by_name(tid: str, name: str) -> Array | None:
    with SessionLocal() as db:
        return db.execute(
            select(Array).where(Array.tenant_id == tid, Array.name == name)
        ).scalars().first()


def test_fronius_capture_with_coords_geocodes_the_array(client):
    tid, key = _mk_tenant()
    body = {
        "provider": "fronius",
        "sites": [{
            "site_id": "PV-LOC-1", "name": "Waterford Fronius",
            "energy_today_kwh": 12.0, "current_power_w": 500,
            "latitude": 44.4337, "longitude": -71.9387,
            "inverters": [],
        }],
    }
    r = client.post(CAPTURE, json=body, headers=_auth(key))
    assert r.status_code == 200, r.text
    arr = _array_by_name(tid, "Waterford Fronius")
    assert arr is not None
    assert arr.latitude is not None and abs(arr.latitude - 44.4337) < 1e-4
    assert arr.longitude is not None and abs(arr.longitude - (-71.9387)) < 1e-4
    assert arr.geocode_source == "vendor:fronius"


def test_sma_capture_with_address_geocodes_via_census(client, monkeypatch):
    # No coords, only an address string — the ingest must geocode it (mock the
    # network call so this test doesn't depend on a live Census/Nominatim hit).
    from api import forecasting
    monkeypatch.setattr(forecasting, "geocode_oneline", lambda oneline: {
        "lat": 44.21, "lng": -72.20, "matched": "2126 SCOTT HWY, GROTON, VT, 05046",
    })
    tid, key = _mk_tenant()
    body = {
        "provider": "sma",
        "sites": [{
            "site_id": "SMA-LOC-1", "name": "Timberworks SMA",
            "energy_today_kwh": 8.0, "current_power_w": 300,
            "address": "2126 Scott Hwy, Groton, VT 05046",
            "inverters": [],
        }],
    }
    r = client.post(CAPTURE, json=body, headers=_auth(key))
    assert r.status_code == 200, r.text
    arr = _array_by_name(tid, "Timberworks SMA")
    assert arr is not None
    assert arr.latitude is not None and abs(arr.latitude - 44.21) < 1e-3
    assert arr.geocode_source == "vendor:sma-geo"
    assert arr.geocoded_address == "2126 SCOTT HWY, GROTON, VT, 05046"


def test_chint_capture_with_no_location_leaves_array_ungeocoded(client):
    # The honest baseline: no lat/lng/address in the payload → no location set,
    # never fabricated.
    tid, key = _mk_tenant()
    body = {
        "provider": "chint",
        "sites": [{
            "site_id": "CH-LOC-1", "name": "No-Location Chint",
            "energy_today_kwh": 5.0, "current_power_w": 200,
            "inverters": [],
        }],
    }
    r = client.post(CAPTURE, json=body, headers=_auth(key))
    assert r.status_code == 200, r.text
    arr = _array_by_name(tid, "No-Location Chint")
    assert arr is not None
    assert arr.latitude is None and arr.geocode_source is None


def test_capture_never_overwrites_an_existing_location(client):
    # A manual override (or an earlier utility-address geocode) must survive a
    # later vendor capture that tries to set a DIFFERENT location — additive
    # only, never a silent overwrite.
    tid, key = _mk_tenant()
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Already Located Fronius",
                    latitude=1.0, longitude=2.0, geocode_source="manual")
        db.add(arr); db.commit()
    body = {
        "provider": "fronius",
        "sites": [{
            "site_id": "PV-LOC-2", "name": "Already Located Fronius",
            "energy_today_kwh": 3.0, "current_power_w": 100,
            "latitude": 44.0, "longitude": -72.0,
            "inverters": [],
        }],
    }
    r = client.post(CAPTURE, json=body, headers=_auth(key))
    assert r.status_code == 200, r.text
    arr2 = _array_by_name(tid, "Already Located Fronius")
    assert arr2.latitude == 1.0 and arr2.longitude == 2.0
    assert arr2.geocode_source == "manual"
