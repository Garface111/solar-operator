"""P2 stubs — documented future work only (not multi-tenant O&M / SLA this wave).

These are intentionally thin: raise NotImplementedError or return
``{"status": "planned", ...}`` so product maps and agents know the intent
without shipping unfinished surfaces.
"""
from __future__ import annotations

from typing import Any

# ── O&M multi-tenant (P2) ────────────────────────────────────────────────────
OM_MULTI_TENANT_STUB: dict[str, Any] = {
    "status": "planned",
    "name": "O&M multi-tenant verification",
    "description": (
        "Allow an O&M / service provider tenant to view Performance Verification "
        "across multiple owner fleets they manage (read-scoped by assignment), "
        "without mixing billing or offtaker data. Not in this wave — Array "
        "Operator owners only for monthly verification reports."
    ),
    "future_endpoints": [
        "GET /v1/om/verification/summary",
        "GET /v1/om/verification/tenants/{tenant_id}/arrays/{array_id}",
        "GET /v1/om/verification/report?period=YYYY-MM",
        "GET /v1/om/verification/assignments",
        "PUT /v1/om/verification/assignments/{owner_tenant_id}",
    ],
    "non_goals_this_wave": [
        "No multi-tenant O&M UI",
        "No cross-tenant write paths",
        "No SLA packaging in the owner SPA",
    ],
}

# ── SLA packaging (P2) ───────────────────────────────────────────────────────
SLA_PACKAGING_STUB: dict[str, Any] = {
    "status": "planned",
    "name": "SLA packaging",
    "description": (
        "Package portfolio PI, availability, and deviation persistence into "
        "owner- or offtaker-facing SLA evidence packs (contractual uptime / "
        "performance guarantees). Depends on stable verification history and "
        "optional contractual threshold fields not added this wave."
    ),
    "future_endpoints": [
        "GET /v1/array-owners/verification/sla?period=YYYY-MM",
        "GET /v1/array-owners/verification/sla.pdf?period=YYYY-MM",
        "PUT /v1/array-owners/verification/sla-settings",
    ],
    "notes": (
        "Auditor CSV export ships now under /verification/auditor-export; "
        "SLA is a separate packaging layer on top of the same engine."
    ),
}


def om_multi_tenant_status() -> dict[str, Any]:
    """Return planned O&M multi-tenant capability description."""
    return dict(OM_MULTI_TENANT_STUB)


def sla_packaging_status() -> dict[str, Any]:
    """Return planned SLA packaging capability description."""
    return dict(SLA_PACKAGING_STUB)


def om_verification_summary(*_args, **_kwargs) -> dict[str, Any]:
    """Future: O&M portfolio verification across assigned owner fleets."""
    raise NotImplementedError(
        "O&M multi-tenant verification is planned (P2); not available this wave. "
        "See OM_MULTI_TENANT_STUB / product_map verification."
    )


def build_sla_pack(*_args, **_kwargs) -> dict[str, Any]:
    """Future: contractual SLA evidence pack from verification history."""
    raise NotImplementedError(
        "SLA packaging is planned (P2); not available this wave. "
        "Use auditor-export for raw evidence. See SLA_PACKAGING_STUB."
    )


def planned_capabilities() -> list[dict[str, Any]]:
    """List P2 stubs for support maps / agents."""
    return [om_multi_tenant_status(), sla_packaging_status()]
