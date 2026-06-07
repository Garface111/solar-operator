"""SolarEdge integration endpoints.

POST   /v1/account/clients/{client_id}/arrays/{array_id}/solaredge
    Body: {"api_key": str, "site_id"?: int}
    Validates key via SolarEdge API, stores credentials on Array.
    If site_id omitted and account-level key, returns site list for picker.

GET    /v1/account/clients/{client_id}/arrays/{array_id}/solaredge/preview
    Pulls last 7 days immediately. Returns sample rows.

DELETE /v1/account/clients/{client_id}/arrays/{array_id}/solaredge
    Clears api_key and site_id. Leaves DailyGeneration rows intact.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from .account import tenant_from_session, require_not_demo, require_not_demo
from .adapters.solaredge import (
    SolarEdgeAuthError,
    SolarEdgeError,
    fetch_daily_energy,
    list_sites,
    site_details,
)
from .db import SessionLocal
from .jobs.solaredge_pull import pull_daily_for_array
from .models import Array, Client

log = logging.getLogger(__name__)

router = APIRouter()


class SolarEdgeSetupBody(BaseModel):
    api_key: str
    site_id: Optional[int] = None


def _resolve_array_for_client(
    db, tenant_id: str, client_id: int, array_id: int
) -> Array:
    c = db.get(Client, client_id)
    if not c or c.tenant_id != tenant_id:
        raise HTTPException(404, "Client not found")
    arr = db.get(Array, array_id)
    if not arr or arr.tenant_id != tenant_id or arr.client_id != client_id:
        raise HTTPException(404, "Array not found")
    return arr


@router.post("/v1/account/clients/{client_id}/arrays/{array_id}/solaredge")
def setup_solaredge(
    client_id: int,
    array_id: int,
    body: SolarEdgeSetupBody,
    authorization: Optional[str] = Header(default=None),
):
    """Connect a SolarEdge site to an array.

    If site_id is provided: validates immediately via site_details().
    If omitted: calls list_sites() — returns site list so the UI can
    show a picker. Saves credentials only when site_id is known.
    """
    t = tenant_from_session(authorization)
    require_not_demo(t)
    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    if body.site_id is None:
        # Account-level key — let the operator pick the site.
        try:
            sites = list_sites(api_key)
        except SolarEdgeError as exc:
            raise HTTPException(400, f"SolarEdge error: {exc}")

        if len(sites) == 1:
            # Auto-select the only site — skip the picker.
            chosen = sites[0]
            site_id = chosen["site_id"]
        elif len(sites) > 1:
            return {
                "ok": True,
                "needs_site_selection": True,
                "sites": sites,
            }
        else:
            # Site-level key or 0 sites returned — require explicit site_id.
            return {
                "ok": True,
                "needs_site_selection": True,
                "sites": [],
                "hint": "Site-level key detected. Please enter the SolarEdge site ID manually.",
            }
    else:
        site_id = body.site_id

    # Validate the key against the chosen site.
    try:
        details = site_details(api_key, site_id)
    except SolarEdgeAuthError as exc:
        raise HTTPException(400, str(exc))
    except SolarEdgeError as exc:
        raise HTTPException(400, f"SolarEdge error: {exc}")

    with SessionLocal() as db:
        arr = _resolve_array_for_client(db, t.id, client_id, array_id)
        arr.solaredge_api_key = api_key
        arr.solaredge_site_id = site_id
        db.commit()

    return {
        "ok": True,
        "needs_site_selection": False,
        "site_name": details["name"],
        "peak_kw": details["peak_kw"],
        "site_id": site_id,
    }


@router.get("/v1/account/clients/{client_id}/arrays/{array_id}/solaredge/preview")
def preview_solaredge(
    client_id: int,
    array_id: int,
    authorization: Optional[str] = Header(default=None),
):
    """Pull the last 7 days immediately. Returns a sample for UI confirmation."""
    t = tenant_from_session(authorization)

    with SessionLocal() as db:
        arr = _resolve_array_for_client(db, t.id, client_id, array_id)
        if not arr.solaredge_api_key or not arr.solaredge_site_id:
            raise HTTPException(400, "Array does not have SolarEdge credentials configured")

        result = pull_daily_for_array(db, array_id, days_back=7)

    if result["errors"]:
        raise HTTPException(502, f"SolarEdge pull failed: {result['errors'][0]}")

    # Re-query to get the actual rows for the sample (pull_daily_for_array committed)
    from datetime import date, timedelta
    from sqlalchemy import select as sa_select
    from .models import DailyGeneration

    with SessionLocal() as db:
        sample_rows = db.execute(
            sa_select(DailyGeneration.day, DailyGeneration.kwh)
            .where(DailyGeneration.array_id == array_id)
            .order_by(DailyGeneration.day.desc())
            .limit(3)
        ).all()

    sample = [
        {"day": str(row.day), "kwh": round(row.kwh, 3)}
        for row in sample_rows
    ]

    return {
        "ok": True,
        "days_pulled": result["days_pulled"],
        "sample": sample,
    }


@router.delete("/v1/account/clients/{client_id}/arrays/{array_id}/solaredge")
def disconnect_solaredge(
    client_id: int,
    array_id: int,
    authorization: Optional[str] = Header(default=None),
):
    """Clear SolarEdge credentials from this array.

    DailyGeneration rows with source='solaredge' are NOT deleted — historical
    data is preserved. Only future pulls are disabled.
    """
    t = tenant_from_session(authorization)
    require_not_demo(t)

    with SessionLocal() as db:
        arr = _resolve_array_for_client(db, t.id, client_id, array_id)
        arr.solaredge_api_key = None
        arr.solaredge_site_id = None
        db.commit()

    return {"ok": True}
