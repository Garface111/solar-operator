"""Eligibility for the generation-reports pipeline (THE FOLD, Phase 1).

Historically the NEPOOL report jobs decided who gets scheduled sends and
operator digests by PRODUCT (`Tenant.product == "nepool"`, skip
"array_operator"). After the fold (NEPOOL Operator -> Array Operator
"Generation reports"), migrated AO tenants must keep receiving scheduled
sends, pre-send reviews, delivery receipts and the operator NEPOOL-GIS
directory.

WHY AN EXPLICIT MARKER AND NOT "has clients + cadence" INFERENCE
----------------------------------------------------------------
The obvious data-presence predicate — tenant has active Client rows and a
report cadence — is UNSAFE, proven on prod ground truth (read-only probe,
2026-07-16): **47 non-demo array_operator tenants already have live active
Client rows**, because the AO capture path auto-creates a Client per
utility-login holder (the "SolarEdge owner" sibling-client pattern), and
every tenant carries the default quarterly cadence. Inference would have
started mailing pre-send reviews and quarterly workbook attempts to dozens of
real + test AO operators (incl. ten_demo_realistic with 97 capture-clients)
on the next cron tick.

So the AO arm keys on the EXPLICIT ``Tenant.generation_reports`` marker that
``scripts/migrate_nepool_tenant.py`` flips when it moves a NEPOOL tenant's
reports world onto its AO sibling, or that an AO operator flips themselves via
``POST /v1/account/generation-reports/enable`` (desk-only — does not set any
client's ``auto_send`` or create charges; contrast
``POST /v1/account/clients/auto-send-all`` which enrolls every client):

  * product != "array_operator"  -> in the reports world (legacy NEPOOL
                                    behavior, unchanged)
  * product == "array_operator"  -> in the reports world IFF
                                    tenant.generation_reports is True

On top of that, a send-eligible tenant must be standing (active, or
comped/trialing) and NOT the shared demo tenant. The demo exclusion is
LOAD-BEARING: ``api/seed_demo.py`` seeds Client rows on the AO demo tenant;
under the old code the product gate was what kept it out of digests.

Behavior-neutral in prod today: generation_reports is FALSE for every tenant
until Phase 4 runs the migration, so every AO tenant — including the 47 with
capture-created clients — stays exactly as gated as under the old product
checks.
"""
from __future__ import annotations

from sqlalchemy import select

from .models import Client


def tenant_in_reports_world(tenant) -> bool:
    """Is this tenant's generation-reports surface live at all?

    NEPOOL (and legacy/unknown-product) tenants: always — unchanged behavior.
    Array Operator tenants: only when the fold migration flipped
    ``generation_reports`` (see module docstring for why this is an explicit
    marker rather than clients+cadence inference).
    """
    if tenant is None:
        return False
    if getattr(tenant, "product", "nepool") != "array_operator":
        return True
    return bool(getattr(tenant, "generation_reports", False))


def tenant_reports_eligible(tenant) -> bool:
    """Tenant-level eligibility for generation-report sends and digests.

    In the reports world (see tenant_in_reports_world) + standing (active, or
    comped/trialing) + not the shared demo tenant. Callers establish the
    client/data presence themselves (their queries join on Client /
    ReportDelivery rows).
    """
    if tenant is None:
        return False
    if getattr(tenant, "is_demo", False):
        return False
    if not tenant_in_reports_world(tenant):
        return False
    return bool(
        getattr(tenant, "active", False)
        or getattr(tenant, "subscription_status", None) in ("comped", "trialing")
    )


def tenant_has_report_clients(db, tenant_id: str) -> bool:
    """Data-presence test: does this tenant have >=1 live, active Client row?

    Used WITH tenant_in_reports_world on the directory path — the reports
    world decides whether the surface exists, client presence decides whether
    there is anything to render.
    """
    row = db.execute(
        select(Client.id).where(
            Client.tenant_id == tenant_id,
            Client.active == True,  # noqa: E712
            Client.deleted_at.is_(None),
        ).limit(1)
    ).first()
    return row is not None
