"""Data-presence eligibility for the generation-reports pipeline (THE FOLD, Phase 1).

Historically the NEPOOL report jobs decided who gets scheduled sends and
operator digests by PRODUCT (`Tenant.product == "nepool"`, skip
"array_operator"). After the fold (NEPOOL Operator -> Array Operator
"Generation reports"), migrated AO tenants carry Client rows + a report
cadence and must keep receiving scheduled sends, pre-send reviews, delivery
receipts and the operator NEPOOL-GIS directory. Eligibility is therefore
keyed on DATA PRESENCE, never on product:

  * the tenant is standing (active, or comped/trialing), AND
  * the tenant is NOT the shared read-only demo tenant, AND
  * the surface's own data joins establish the client presence
    (scheduler/pre-send join on Client rows by cadence; receipts key on the
    ReportDelivery rows the scheduler wrote; the directory checks
    ``tenant_has_report_clients``).

The demo exclusion is LOAD-BEARING, not defensive polish: ``api/seed_demo.py``
seeds Client rows on the AO demo tenant (active=True, default quarterly
cadence). Under the old code the ``product == "array_operator"`` skip was what
kept the demo tenant out of pre-send reviews and delivery receipts — remove
the product gate without excluding demo and the demo mailbox starts receiving
quarterly operator digests. ``tenant_reports_eligible`` carries that guard.

Behavior-neutral in prod today: no REAL (non-demo) AO tenant has Client rows
until the Phase-4 migration runs, so for every current AO tenant the
data-presence predicate is exactly as false as the old product gate.
"""
from __future__ import annotations

from sqlalchemy import select

from .models import Client


def tenant_reports_eligible(tenant) -> bool:
    """Tenant-level eligibility for generation-report sends and digests.

    True when the tenant is standing (active, or on a comped/trialing
    subscription) and is not the shared demo tenant. Deliberately says
    NOTHING about product — post-fold, an Array Operator tenant with report
    clients is as eligible as a NEPOOL one. Callers establish the client/data
    presence themselves (their queries join on Client / ReportDelivery rows).
    """
    if tenant is None:
        return False
    if getattr(tenant, "is_demo", False):
        return False
    return bool(
        getattr(tenant, "active", False)
        or getattr(tenant, "subscription_status", None) in ("comped", "trialing")
    )


def tenant_has_report_clients(db, tenant_id: str) -> bool:
    """Data-presence test: does this tenant have >=1 live, active Client row?

    This is the seam that replaces ``product == "nepool"`` checks on the
    report path (operator directory, directory download/send endpoints).
    A tenant with no clients has no generation-reports world — whatever its
    product — and one WITH clients gets the full report surface.
    """
    row = db.execute(
        select(Client.id).where(
            Client.tenant_id == tenant_id,
            Client.active == True,  # noqa: E712
            Client.deleted_at.is_(None),
        ).limit(1)
    ).first()
    return row is not None
