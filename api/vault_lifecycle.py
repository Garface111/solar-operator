"""Cloud Capture vault lifecycle — churn teardown + account deletion helpers.

Hard-delete only (never soft-delete secrets). Shared by cancel/churn paths,
customer account-deletion, and ops scripts so we never leave opted-in portal
passwords harvestable after the relationship ends.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text

log = logging.getLogger("solar.vault_lifecycle")

# Child tables that hold Cloud Capture / portal session crown jewels for a tenant.
# Order matters when FKs exist; most are tenant_id only with no cascade.
_VAULT_TABLES = (
    "harvest_run",
    "portal_credential",
    "portal_login_status",
)


def teardown_cloud_capture_for_tenant(db, tenant_id: str, *, reason: str = "") -> dict[str, Any]:
    """Hard-delete all Cloud Capture vault rows for a tenant.

    Removes:
      * portal_credential (secret_enc + session_state_enc)
      * harvest_run history
      * portal_login_status roster rows for that tenant

    Idempotent. Never logs secrets. Returns counts only.
    """
    tid = (tenant_id or "").strip()
    if not tid:
        return {"ok": False, "error": "tenant_id required"}

    counts: dict[str, int] = {}
    # Prefer ORM when models are mapped so Encrypted* types stay out of logs.
    try:
        from .models import HarvestRun, PortalCredential, PortalLoginStatus

        n_hr = db.execute(
            select(HarvestRun).where(HarvestRun.tenant_id == tid)
        ).scalars().all()
        for r in n_hr:
            db.delete(r)
        counts["harvest_run"] = len(n_hr)

        n_pc = db.execute(
            select(PortalCredential).where(PortalCredential.tenant_id == tid)
        ).scalars().all()
        for r in n_pc:
            # Clear sensitive fields before delete in case of deferred flush / session expire
            r.secret_enc = None
            r.session_state_enc = None
            db.delete(r)
        counts["portal_credential"] = len(n_pc)

        n_pl = db.execute(
            select(PortalLoginStatus).where(PortalLoginStatus.tenant_id == tid)
        ).scalars().all()
        for r in n_pl:
            db.delete(r)
        counts["portal_login_status"] = len(n_pl)

        db.flush()
    except Exception as exc:  # noqa: BLE001 — fall back to raw SQL
        log.warning("ORM vault teardown failed for %s (%s); trying SQL", tid, type(exc).__name__)
        bind = db.get_bind()
        with bind.begin() as conn:
            for table in _VAULT_TABLES:
                try:
                    r = conn.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                        {"tid": tid},
                    )
                    counts[table] = int(r.rowcount or 0)
                except Exception as e2:  # noqa: BLE001
                    counts[table] = -1
                    log.warning("SQL teardown %s tenant=%s: %s", table, tid, type(e2).__name__)

    log.info(
        "cloud_capture_teardown tenant=%s reason=%s counts=%s",
        tid, (reason or "")[:80], counts,
    )
    return {"ok": True, "tenant_id": tid, "reason": (reason or "")[:200], "counts": counts}


def disable_cloud_capture_for_tenant(db, tenant_id: str) -> int:
    """Flip cloud_capture_enabled=False without deleting (pre-teardown belt)."""
    from .models import PortalCredential

    rows = db.execute(
        select(PortalCredential).where(PortalCredential.tenant_id == tenant_id)
    ).scalars().all()
    for r in rows:
        r.cloud_capture_enabled = False
        r.secret_enc = None
        r.session_state_enc = None
        r.session_state_at = None
    db.flush()
    return len(rows)


def purge_tenant_sensitive_data(db, tenant_id: str, *, reason: str = "") -> dict[str, Any]:
    """Hard-delete vault + utility session JWTs for a GDPR-style data wipe.

    Does not drop the tenants row (FK safety) — caller deactivates/anonymizes.
    """
    out = teardown_cloud_capture_for_tenant(db, tenant_id, reason=reason)
    counts = dict(out.get("counts") or {})
    try:
        from .models import UtilitySession

        rows = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == tenant_id)
        ).scalars().all()
        for r in rows:
            r.api_token = None
            r.refresh_token = None
            r.raw_payload = None
            db.delete(r)
        counts["utility_sessions"] = len(rows)
        db.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("utility_session purge failed tenant=%s: %s", tenant_id, type(exc).__name__)
        counts["utility_sessions"] = -1
    out["counts"] = counts
    return out
