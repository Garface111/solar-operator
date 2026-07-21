"""Weather-model nameplate for SolarEdge arrays with no stamped inverter kW.

SolarEdge gives us site peakPower + model-encoded capacity. After connect we
may have location + generation days but zero Inverter rows (fleet inventory
hasn't run). _array_nameplate_kw must still resolve capacity so Analysis does
not stick on "not modeled yet".
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.array_owners import _array_nameplate_kw, _attach_solaredge
from api.db import SessionLocal
from api.models import Array, Inverter, InverterConnection, Tenant


def _tenant_array(name: str = "SE Site") -> tuple[str, int]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="NP Test", contact_email=f"{tid}@t.test",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.flush()
        arr = Array(
            tenant_id=tid, name=name,
            latitude=43.6, longitude=-72.3, geocode_source="vendor:solaredge-geo",
        )
        db.add(arr)
        db.flush()
        arr_id = arr.id
        db.commit()
    return tid, arr_id


def test_nameplate_from_connection_peak_power_when_no_inverters():
    _tid, arr_id = _tenant_array("Cover Rooftop")
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        _attach_solaredge(db, arr, "fake-key", 4631514, peak_power_kw=10.0)
        db.commit()
        assert _array_nameplate_kw(db, arr) == 10.0


def test_nameplate_from_array_name_kw_token():
    """Last-resort parse of 'Starlake 45kW SolarEdge' when no peak stamped."""
    _tid, arr_id = _tenant_array("Starlake 45kW SolarEdge")
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        # Connection without peak_power_kw
        db.add(InverterConnection(
            array_id=arr.id, vendor="solaredge",
            config={"api_key": "k", "site_id": 1}, status="ok",
        ))
        db.commit()
        assert _array_nameplate_kw(db, arr) == 45.0


def test_inverter_sum_beats_peak_fallback():
    _tid, arr_id = _tenant_array("Mixed")
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        _attach_solaredge(db, arr, "fake-key", 1, peak_power_kw=99.0)
        db.add(Inverter(
            tenant_id=arr.tenant_id, array_id=arr.id, position=0,
            vendor="solaredge", serial="S1", model="SE10000", nameplate_kw=10.0,
        ))
        db.add(Inverter(
            tenant_id=arr.tenant_id, array_id=arr.id, position=1,
            vendor="solaredge", serial="S2", model="SE5000", nameplate_kw=None,
        ))
        db.commit()
        # 10 stored + 5 from model parse (SE5000), not the 99 peak fallback
        assert _array_nameplate_kw(db, arr) == 15.0


def test_attach_preserves_prior_peak_when_rediscover_omits():
    _tid, arr_id = _tenant_array("Preserve")
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        _attach_solaredge(db, arr, "k1", 1, peak_power_kw=45.0)
        db.commit()
        _attach_solaredge(db, arr, "k2", 1, peak_power_kw=None)
        db.commit()
        conn = db.execute(
            select(InverterConnection).where(InverterConnection.array_id == arr_id)
        ).scalar_one()
        assert float(conn.config["peak_power_kw"]) == 45.0
        assert conn.config["api_key"] == "k2"
