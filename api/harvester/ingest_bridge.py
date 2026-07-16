"""Deliver scraped data through the EXACT capture endpoints the extension uses.

The harvester shares the database, so it resolves the tenant's own Bearer key
(Tenant.tenant_key, the `sol_live_...` activation key the capture routes accept)
and POSTs to the running API — /v1/sync, /v1/array-owners/utility-meter-capture,
/v1/array-owners/inverter-capture — with byte-identical payloads. Feeding the
same routes means the same dup-safety, plausibility guards, UtilitySession
storage and bill-PDF backend enumeration run unchanged; there is no second,
less-careful write path to drift from the extension.
"""
from __future__ import annotations

import logging
import os

import httpx
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant
from .vendors.base import CaptureRequest

log = logging.getLogger("harvester.ingest")


def api_base() -> str:
    """Base URL of the Array Operator API the captures POST to. Defaults to the
    same PROD endpoint the extension ships with; override with AO_API_BASE (e.g.
    the Railway internal service URL to avoid a public round-trip)."""
    return (os.environ.get("AO_API_BASE") or "https://arrayoperator.com").rstrip("/")


def _tenant_key(tenant_id: str) -> str | None:
    with SessionLocal() as db:
        t = db.execute(
            select(Tenant.tenant_key).where(Tenant.id == tenant_id)
        ).scalar_one_or_none()
        return t


async def deliver(tenant_id: str, requests: list[CaptureRequest]) -> int:
    """POST each CaptureRequest with the tenant's Bearer key. Returns how many
    succeeded (2xx). A single failed POST does not abort the rest — partial
    delivery is still progress and is reflected in rows_written."""
    if not requests:
        return 0
    key = _tenant_key(tenant_id)
    if not key:
        log.warning("no tenant_key for %s — cannot deliver %d captures", tenant_id, len(requests))
        return 0

    base = api_base()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    ok = 0
    # A generous timeout: bill payloads carry base64 PDFs and the backend does
    # real ingest work. Kept off the DB-session path (we hold none here).
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        for req in requests:
            url = f"{base}{req.path}"
            try:
                resp = await client.request(req.method, url, headers=headers, json=req.body)
                if 200 <= resp.status_code < 300:
                    ok += 1
                else:
                    log.warning("ingest %s -> %s (%s)", req.note or req.path,
                                resp.status_code, (resp.text or "")[:160])
            except Exception as exc:                       # noqa: BLE001
                log.warning("ingest %s failed: %s", req.note or req.path, exc)
    return ok
