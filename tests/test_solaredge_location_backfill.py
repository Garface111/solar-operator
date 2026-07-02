"""SolarEdge location was only ever captured ONCE, at initial connect — an
array connected before that code existed (or whose first capture missed it)
was stuck on "set location" forever with no other chance to pick it up (Ford,
2026-07-02: "still not working for ... SolarEdge"). api.jobs.inverter_pull's
daily pull (which runs for EVERY connected array regardless of connect date)
now piggybacks a location backfill so this self-heals automatically.
"""
from __future__ import annotations

import secrets
from datetime import date
from unittest.mock import patch

from api.db import SessionLocal
from api.jobs.inverter_pull import pull_all_inverters
from api.models import Array, InverterConnection, Tenant


def _mk_solaredge_array(with_location: bool = False) -> tuple[str, int]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="SE Loc Test", contact_email=f"{tid}@t.test",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.flush()
        kwargs = {}
        if with_location:
            kwargs = {"latitude": 1.0, "longitude": 2.0, "geocode_source": "manual"}
        arr = Array(tenant_id=tid, name="Old SolarEdge Array", **kwargs)
        db.add(arr)
        db.flush()
        db.add(InverterConnection(
            array_id=arr.id, vendor="solaredge",
            config={"api_key": "fake_key", "site_id": 99999}, status="ok",
        ))
        arr_id = arr.id
        db.commit()
    return tid, arr_id


def _array(arr_id: int) -> Array:
    with SessionLocal() as db:
        return db.get(Array, arr_id)


def test_daily_pull_backfills_missing_solaredge_location():
    _tid, arr_id = _mk_solaredge_array(with_location=False)
    with patch("api.inverters.solaredge.fetch_daily", return_value=[
        {"day": date(2026, 6, 30), "kwh": 10.0},
    ]), patch("api.jobs.inverter_pull._se.site_details", return_value={
        "site_id": 99999, "name": "Old Site", "peak_kw": 10.0,
        "address": "158 S Main St, White River Junction, VT", "status": "Active",
    }), patch("api.forecasting.geocode_oneline", return_value={
        "lat": 43.6464, "lng": -72.3186, "matched": "158 S MAIN ST, WHITE RIVER JUNCTION, VT",
    }):
        result = pull_all_inverters(days_back=1)

    # Scope the error check to THIS test's array. pull_all_inverters walks every
    # connected array in the shared test DB, so a fixture left by an earlier test
    # can add its own error row — that's unrelated to whether OUR backfill worked.
    mine = [r for r in result["results"] if r.get("array_id") == arr_id]
    assert mine, "our array was not processed by the daily pull"
    assert not any(r.get("errors") for r in mine)
    arr = _array(arr_id)
    assert arr.latitude is not None and abs(arr.latitude - 43.6464) < 1e-3
    assert arr.geocode_source == "vendor:solaredge-geo"


def test_daily_pull_never_overwrites_an_existing_location():
    _tid, arr_id = _mk_solaredge_array(with_location=True)
    with patch("api.inverters.solaredge.fetch_daily", return_value=[
        {"day": date(2026, 6, 30), "kwh": 10.0},
    ]), patch("api.jobs.inverter_pull._se.site_details", return_value={
        "site_id": 99999, "name": "Old Site", "peak_kw": 10.0,
        "address": "Some Other Address, VT", "status": "Active",
    }) as mock_details:
        pull_all_inverters(days_back=1)
        # site_details is never even called when the array already has a location —
        # the backfill hook short-circuits before touching the network.
        mock_details.assert_not_called()

    arr = _array(arr_id)
    assert arr.latitude == 1.0 and arr.longitude == 2.0
    assert arr.geocode_source == "manual"


def test_site_details_failure_never_breaks_the_daily_pull():
    _tid, arr_id = _mk_solaredge_array(with_location=False)
    with patch("api.inverters.solaredge.fetch_daily", return_value=[
        {"day": date(2026, 6, 30), "kwh": 10.0},
    ]), patch("api.jobs.inverter_pull._se.site_details", side_effect=RuntimeError("boom")):
        result = pull_all_inverters(days_back=1)

    # The daily energy pull itself must still have succeeded — scope to OUR array
    # (the shared DB may hold other arrays from earlier tests; result[0] isn't
    # guaranteed to be ours).
    mine = next(r for r in result["results"] if r.get("array_id") == arr_id)
    assert mine["days_pulled"] == 1
    assert not mine.get("errors")
    arr = _array(arr_id)
    assert arr.latitude is None   # backfill failed silently, nothing fabricated
