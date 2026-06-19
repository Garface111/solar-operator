"""Duplicate-array auto-dedup sweep.

Finds GMP↔vendor (and other) duplicate arrays per tenant and merges the
unambiguous ones automatically, leaving anything questionable as a one-click
suggestion in the UI. DRY-RUN by default — nothing commits unless execute=True.

POLICY (set with Ford Jun'26):
  • STRONG pairs (shared utility account / identical normalized name / same
    NEPOOL id) → auto-merge.
  • MEDIUM pairs (one name contains the other, cross-source vendor+GMP, and NOT a
    sub-array token split) → auto-merge.
  • WEAK pairs → never auto-merged; surfaced via the existing merge-suggestion UI.
  • Sub-array token splits (Starlake North vs South) → never even considered
    (dropped in find_duplicate_pairs).

Safe to run repeatedly: merges are idempotent, soft (undo rows written), and a
once-merged src array is soft-deleted so it won't be reconsidered.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant
from ..array_merge import find_duplicate_pairs, merge_arrays

log = logging.getLogger(__name__)

AUTO_TIERS = {"STRONG", "MEDIUM"}


def sweep_tenant(tenant_id: str, *, execute: bool = False,
                 db=None) -> dict:
    """Detect + (optionally) merge duplicate arrays for one tenant.

    Returns {tenant_id, pairs, auto_merged:[...], suggested:[...]}.
    With execute=False (default) auto_merged lists what WOULD merge (dry run).
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        pairs = find_duplicate_pairs(db, tenant_id)
        auto_merged: list[dict] = []
        suggested: list[dict] = []
        merged_src_ids: set[int] = set()
        merged_into: dict[int, int] = {}   # src that became dst-of-another → follow

        for p in pairs:
            if p["confidence"] not in AUTO_TIERS:
                suggested.append(p)
                continue
            src_id, dst_id = p["src_id"], p["dst_id"]
            # If either side was already merged away this sweep, skip — the next
            # sweep re-scores from the surviving arrays (keeps each op clean).
            if src_id in merged_src_ids or dst_id in merged_src_ids:
                continue
            if not execute:
                auto_merged.append({**p, "executed": False})
                continue
            res = merge_arrays(db, src_id, dst_id, tenant_id,
                               reason=f"auto-dedup:{p['confidence'].lower()}")
            if res.get("ok") and not res.get("noop"):
                merged_src_ids.add(src_id)
                auto_merged.append({**p, "executed": True,
                                    "undo_token": res.get("undo_token"),
                                    "counts": res.get("counts")})
        if execute and auto_merged:
            db.commit()
        return {"tenant_id": tenant_id, "pairs": len(pairs),
                "auto_merged": auto_merged, "suggested": suggested}
    finally:
        if own:
            db.close()


def sweep_all_tenants(*, execute: bool = False) -> dict:
    """Run the dedup sweep across every active tenant.

    DRY-RUN by default. The scheduled job calls this with execute=True for STRONG/
    MEDIUM tiers only (the policy lives in sweep_tenant). Returns grand totals +
    a per-tenant breakdown for any tenant that had candidates.
    """
    with SessionLocal() as db:
        tenant_ids = list(db.execute(
            select(Tenant.id).where(Tenant.active == True)  # noqa: E712
        ).scalars().all())
    grand = {"tenants_scanned": 0, "tenants_with_dupes": 0,
             "auto_merged": 0, "suggested": 0, "details": []}
    for tid in tenant_ids:
        r = sweep_tenant(tid, execute=execute)
        grand["tenants_scanned"] += 1
        n_auto = len(r["auto_merged"])
        n_sugg = len(r["suggested"])
        if n_auto or n_sugg:
            grand["tenants_with_dupes"] += 1
            grand["auto_merged"] += n_auto
            grand["suggested"] += n_sugg
            grand["details"].append({
                "tenant_id": tid,
                "auto_merged": [
                    {"src": p["src_id"], "src_name": p["src_name"],
                     "dst": p["dst_id"], "dst_name": p["dst_name"],
                     "confidence": p["confidence"], "executed": p.get("executed", False)}
                    for p in r["auto_merged"]
                ],
                "suggested": [
                    {"a": p["a_id"], "b": p["b_id"], "confidence": p["confidence"],
                     "reasons": p["reasons"]}
                    for p in r["suggested"]
                ],
            })
    log.info("array dedup sweep (execute=%s): auto_merged=%d suggested=%d across %d tenants",
             execute, grand["auto_merged"], grand["suggested"], grand["tenants_with_dupes"])
    return grand
