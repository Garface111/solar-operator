"""
Writer registry — dispatch the workbook builder by Array.fuel_type.

The public ``build_workbook`` exported here is a thin dispatcher with the SAME
signature every caller already uses (``from api.writers import build_workbook``).
It resolves the fuel type for the client being reported on and routes to the
matching writer:

    WRITERS = {
        'solar':    gmcs_writer.build_workbook,   # the sacred, pixel-matched
                                                  # GMCS solar report — output
                                                  # stays byte-for-byte identical
        'wind':     rec_writer.build_workbook,
        'hydro':    rec_writer.build_workbook,
        'digester': rec_writer.build_workbook,
        'storage':  rec_writer.build_workbook,
    }

Fuel resolution is defensive: it reads ``Array.fuel_type`` via getattr with a
'solar' default, so this is correct whether or not the fuel_type column has
landed yet, and ALWAYS falls back to the solar (GMCS) writer when the fuel can't
be determined — so the solar path is never disturbed.
"""
from __future__ import annotations

import pathlib
from datetime import date
from typing import Callable, Optional

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Array, Client
from . import gmcs_writer
from . import rec_writer

DEFAULT_FUEL = "solar"

# fuel_type → writer entrypoint. 'solar' MUST route to the GMCS writer so its
# output remains pixel-identical to Bruce's master.
WRITERS: dict[str, Callable[..., pathlib.Path]] = {
    "solar": gmcs_writer.build_workbook,
    "wind": rec_writer.build_workbook,
    "hydro": rec_writer.build_workbook,
    "digester": rec_writer.build_workbook,
    "storage": rec_writer.build_workbook,
}


def _resolve_fuel(client_id: Optional[int], tenant_id: Optional[str]) -> str:
    """Best-effort fuel type for the client's arrays.

    Returns the first NON-solar fuel found among the client's (non-excluded)
    arrays — that's the fuel whose registry/labels the report should carry. If
    every array is solar (or fuel can't be determined for any reason), returns
    'solar' so dispatch falls back to the untouched GMCS writer.

    This never raises: any error degrades gracefully to the solar path.
    """
    try:
        with SessionLocal() as db:
            cid = client_id
            if cid is None and tenant_id is not None:
                client = db.execute(
                    select(Client).where(Client.tenant_id == tenant_id)
                                  .order_by(Client.id.asc())
                ).scalars().first()
                if client is None:
                    return DEFAULT_FUEL
                cid = client.id
            if cid is None:
                return DEFAULT_FUEL

            arrays = db.execute(
                select(Array).where(
                    Array.client_id == cid,
                    Array.excluded.is_(False),
                )
            ).scalars().all()

            for a in arrays:
                fuel = (getattr(a, "fuel_type", None) or DEFAULT_FUEL).strip().lower()
                if fuel and fuel != DEFAULT_FUEL and fuel in WRITERS:
                    return fuel
            return DEFAULT_FUEL
    except Exception:
        # Never let fuel resolution break report generation — solar is the safe
        # default and keeps the sacred path byte-identical.
        return DEFAULT_FUEL


def build_workbook(tenant_id: Optional[str] = None,
                   year: Optional[int] = None,
                   out_path: Optional[pathlib.Path] = None,
                   *, quarters: int = 6,
                   reference_date: Optional[date] = None,
                   client_id: Optional[int] = None) -> pathlib.Path:
    """Dispatch workbook generation to the writer for the client's fuel type.

    Signature is identical to the underlying writers; all existing callers work
    unchanged. Solar (the default) routes to ``gmcs_writer.build_workbook`` with
    the exact same arguments, so its output is byte-for-byte unchanged.
    """
    fuel = _resolve_fuel(client_id, tenant_id)
    writer = WRITERS.get(fuel, WRITERS[DEFAULT_FUEL])
    return writer(
        tenant_id=tenant_id,
        year=year,
        out_path=out_path,
        quarters=quarters,
        reference_date=reference_date,
        client_id=client_id,
    )
