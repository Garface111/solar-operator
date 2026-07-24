"""Nightly refresh of the Discover pool.

Re-reads every operator's saved logins so newly-commissioned sites appear in
their Discover tab on their own — the operator never re-enters a password just
to see what's new. Curation stays manual; this only keeps the *menu* current.

Per-tenant and per-login failures are contained: a stale Locus password records
its error against that login (the UI shows it inline) and the sweep moves on.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant
from ..report_eligibility import tenant_in_reports_world

log = logging.getLogger(__name__)


def refresh_all_tenants() -> dict:
    """Refresh the candidate pool for every active reports-world tenant."""
    from .. import discovery  # noqa: PLC0415 — avoid import cycle at module load

    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(Tenant.active.is_(True))
        ).scalars().all()
        targets = [t for t in tenants if tenant_in_reports_world(t)]

    scanned = 0
    found = 0
    failed = 0
    for tenant in targets:
        try:
            with SessionLocal() as db:
                result = discovery.refresh_tenant(db, tenant.id)
            scanned += 1
            found += result.get("found", 0)
        except Exception:  # noqa: BLE001 — one tenant must not end the sweep
            failed += 1
            log.exception("discovery_refresh: tenant %s failed", tenant.id)

    log.info(
        "discovery_refresh: tenants=%d candidates=%d failed=%d",
        scanned, found, failed,
    )
    return {"tenants": scanned, "candidates": found, "failed": failed}
