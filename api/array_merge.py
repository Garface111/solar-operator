"""Lossless array merge + smart duplicate detection.

WHY THIS EXISTS
  The GMP data-absorption feature creates one Array per GMP account; the vendor
  connect flow creates one Array per inverter site. The SAME physical array can
  therefore exist TWICE in our system — once from GMP (utility-meter settlement
  data) and once from the vendor (inverter telemetry). This module combines those
  twins into ONE array WITHOUT losing a single row of either side's data.

TWO PUBLIC SURFACES
  • merge_arrays(db, src_id, dst_id, tenant_id, reason) — the LOSSLESS engine.
    Reparents EVERY array-keyed table (not just utility_accounts, which the old
    account.merge_array_into did — that orphaned GMP daily + inverter data), then
    soft-deletes src and writes a DeleteHistory undo row. Handles the two hard
    constraints: DailyGeneration's (array_id, day) unique (source-priority wins on
    a collision) and InverterConnection's unique(array_id).
  • find_duplicate_pairs(db, tenant_id) — scores same-tenant array pairs and tags
    each STRONG (auto-mergeable) / MEDIUM (auto, guarded) / WEAK (suggest only).
    SUB-ARRAY GUARD: names differing only by a directional/positional token
    (north/south/center/roof/lot/...) are NEVER auto-merged — they're real
    distinct sub-meters (e.g. Starlake North vs Starlake South).

The merge DIRECTION is normalized so the richer array survives: the array WITH a
vendor InverterConnection is preferred as the destination (it owns inverters +
live telemetry that can't move as cleanly as utility accounts).
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .models import (
    Array, UtilityAccount, DailyGeneration, GmpDailyGeneration,
    InverterConnection, Inverter, WarrantyClaim, VerificationCheck,
    BillingReportSubscription, ArrayMergeDismissal, DeleteHistory, now,
)

# Real metered sources, strongest first — used to pick the winner when both src
# and dst have a DailyGeneration row for the SAME day (the (array_id,day) unique
# constraint forbids keeping both). Mirrors the absorption priority: a real meter
# beats a bill estimate; an independent inverter feed beats nothing.
_SOURCE_RANK = {
    "solaredge": 9, "fronius": 9, "sma": 9, "chint": 9,
    "extension_pull_corrected": 8, "extension_pull": 7,
    "csv": 6, "manual": 6, "utility_meter": 5,
    "gmp_api": 5, "gmp_portal_scrape": 5, "smarthub": 5,
    "bill_prorate": 1,
}

# Tokens that distinguish REAL sub-arrays sharing a site/stem. If two candidate
# names reduce to the same stem but differ by one of these, they are NOT twins —
# they are separate physical sub-meters and must never be auto-merged.
_SUBARRAY_TOKENS = {
    "north", "south", "east", "west", "center", "centre",
    "upper", "lower", "front", "back", "rear",
    "roof", "rooftop", "carport", "canopy", "ground", "field", "lot",
    "barn", "garage", "shed", "house", "building", "bldg", "wing",
    "a", "b", "c", "d", "1", "2", "3", "4",
    "phase", "array", "block", "section", "unit",
}


def _norm_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r"[,.\-_/()]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _tokens(name: str) -> list[str]:
    return [t for t in _norm_name(name).split(" ") if t]


def _differs_only_by_subarray_token(n1: str, n2: str) -> bool:
    """True if the two names are identical except for ≥1 sub-array token, i.e.
    they're distinct sub-meters of the same site (Starlake North vs South), NOT
    a GMP/vendor twin of one array."""
    t1, t2 = _tokens(n1), _tokens(n2)
    s1, s2 = set(t1), set(t2)
    common = s1 & s2
    diff = (s1 ^ s2)            # tokens in exactly one of the two
    if not diff:
        return False           # identical token sets → not a sub-array split
    if not common:
        return False           # nothing shared → handled elsewhere
    # Every differing token must be a known sub-array token for this to be a
    # safe "these are siblings, don't merge" verdict.
    return all(t in _SUBARRAY_TOKENS for t in diff)


# ─────────────────────────────────────────────────────────────────────────────
# LOSSLESS MERGE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _reparent_daily_generation(db: Session, src_id: int, dst_id: int) -> dict:
    """Move DailyGeneration rows src→dst, resolving (array_id,day) collisions by
    source priority. Returns {moved, resolved_collisions, dropped_weaker}."""
    dst_by_day = {
        r.day: r for r in db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == dst_id)
        ).scalars().all()
    }
    moved = collisions = dropped = 0
    for row in db.execute(
        select(DailyGeneration).where(DailyGeneration.array_id == src_id)
    ).scalars().all():
        clash = dst_by_day.get(row.day)
        if clash is None:
            row.array_id = dst_id
            dst_by_day[row.day] = row
            moved += 1
        else:
            collisions += 1
            src_rank = _SOURCE_RANK.get(row.source or "", 0)
            dst_rank = _SOURCE_RANK.get(clash.source or "", 0)
            if src_rank > dst_rank:
                # src is the stronger feed for this day — keep its value on dst.
                clash.kwh = row.kwh
                clash.source = row.source
                clash.uploaded_at = now()
            # Either way the src row is now redundant; delete it (its data is
            # represented by the surviving dst row).
            db.delete(row)
            dropped += 1
    return {"moved": moved, "resolved_collisions": collisions, "dropped_weaker": dropped}


def _reparent_simple(db: Session, model, src_id: int, dst_id: int,
                     attr: str = "array_id") -> int:
    rows = db.execute(
        select(model).where(getattr(model, attr) == src_id)
    ).scalars().all()
    for r in rows:
        setattr(r, attr, dst_id)
    return len(rows)


def _reparent_inverter_connection(db: Session, src_id: int, dst_id: int) -> dict:
    """InverterConnection has unique(array_id), so dst can hold at most one. Move
    src's connection only if dst has none; otherwise keep dst's and leave src's
    behind to be soft-handled (it dangles on the soft-deleted src array, harmless,
    and preserves its credentials/history for audit)."""
    src_conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == src_id)
    ).scalar_one_or_none()
    if src_conn is None:
        return {"moved": 0, "kept_dst": False}
    dst_conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == dst_id)
    ).scalar_one_or_none()
    if dst_conn is None:
        src_conn.array_id = dst_id
        return {"moved": 1, "kept_dst": False}
    # Both have a connection — dst wins; src_conn stays on the dead src array.
    return {"moved": 0, "kept_dst": True}


def _merge_legacy_solaredge_cols(src: Array, dst: Array) -> None:
    """If dst has no legacy SolarEdge creds but src does, inherit them (keeps the
    virtual-connection daily-pull path working after a merge)."""
    if not dst.solaredge_api_key and src.solaredge_api_key:
        dst.solaredge_api_key = src.solaredge_api_key
        dst.solaredge_site_id = src.solaredge_site_id


def _preferred_destination(db: Session, a: Array, b: Array) -> tuple[Array, Array]:
    """Return (dst, src): the array that should SURVIVE first. Prefer the one
    with a vendor InverterConnection (owns inverters+telemetry), then the one with
    a NEPOOL id, then the older (lower id = more established)."""
    def has_conn(arr):
        return db.execute(
            select(func.count(InverterConnection.id)).where(
                InverterConnection.array_id == arr.id)
        ).scalar() or 0
    a_conn, b_conn = has_conn(a), has_conn(b)
    if a_conn != b_conn:
        return (a, b) if a_conn > b_conn else (b, a)
    if bool(a.nepool_gis_id) != bool(b.nepool_gis_id):
        return (a, b) if a.nepool_gis_id else (b, a)
    return (a, b) if a.id <= b.id else (b, a)


def merge_arrays(db: Session, src_id: int, dst_id: int, tenant_id: str, *,
                 reason: str = "auto-dedup", write_undo: bool = True) -> dict:
    """LOSSLESS merge of src array INTO dst array (both must belong to tenant).

    Reparents every array-keyed table, resolves DailyGeneration day collisions by
    source priority, merges metadata (dst wins when set), soft-deletes src, and
    records a DeleteHistory undo row. Does NOT commit — caller commits (so a batch
    sweep is one transaction). Returns a summary dict.

    Idempotent: a src already soft-deleted returns {"noop": True}.
    """
    src = db.get(Array, src_id)
    dst = db.get(Array, dst_id)
    if src is None or dst is None:
        return {"ok": False, "error": "array not found"}
    if src.tenant_id != tenant_id or dst.tenant_id != tenant_id:
        return {"ok": False, "error": "cross-tenant merge refused"}
    if src.id == dst.id:
        return {"ok": False, "error": "src == dst"}
    if src.deleted_at is not None:
        return {"ok": True, "noop": True, "dst_array_id": dst.id}

    counts = {}
    counts["utility_accounts"] = _reparent_simple(db, UtilityAccount, src.id, dst.id)
    counts["gmp_daily"] = _reparent_simple(db, GmpDailyGeneration, src.id, dst.id)
    counts["daily_generation"] = _reparent_daily_generation(db, src.id, dst.id)
    counts["inverters"] = _reparent_simple(db, Inverter, src.id, dst.id)
    # inverters that point home via source_array_id (for "reset layout")
    counts["inverters_source"] = _reparent_simple(db, Inverter, src.id, dst.id, attr="source_array_id")
    counts["inverter_connection"] = _reparent_inverter_connection(db, src.id, dst.id)
    counts["warranty_claims"] = _reparent_simple(db, WarrantyClaim, src.id, dst.id)
    counts["verification_checks"] = _reparent_simple(db, VerificationCheck, src.id, dst.id)
    counts["billing_subs"] = _reparent_simple(db, BillingReportSubscription, src.id, dst.id)
    # Patch any multi-array allocation JSON that references src.
    for sub in db.execute(select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id)).scalars().all():
        allocs = sub.array_allocations
        if isinstance(allocs, list) and allocs:
            changed = False
            for a in allocs:
                if isinstance(a, dict) and a.get("array_id") == src.id:
                    a["array_id"] = dst.id
                    changed = True
            if changed:
                # reassign so SQLAlchemy detects the JSON mutation
                sub.array_allocations = list(allocs)

    # Metadata: dst keeps its set values, inherits from src where empty.
    for field in ("nepool_gis_id", "region", "first_connect_date",
                  "solar_adder_cents", "notes", "client_id"):
        if getattr(dst, field) in (None, "") and getattr(src, field) not in (None, ""):
            setattr(dst, field, getattr(src, field))
    _merge_legacy_solaredge_cols(src, dst)
    # Excluded only if BOTH were excluded.
    if not (src.excluded and dst.excluded):
        dst.excluded = False

    # Clear dismissals involving src (the pair question is now moot).
    db.query(ArrayMergeDismissal).filter(
        ArrayMergeDismissal.tenant_id == tenant_id,
    ).filter(
        (ArrayMergeDismissal.array_a_id == src.id)
        | (ArrayMergeDismissal.array_b_id == src.id)
    ).delete(synchronize_session=False)

    src.deleted_at = now()
    src.notes = ((src.notes or "") + f"\n[merged into array {dst.id} ({reason}) "
                 f"{now().isoformat()}]")[:2000]

    undo_token = None
    if write_undo:
        undo_token = secrets.token_hex(8)
        db.add(DeleteHistory(
            tenant_id=tenant_id, undo_token=undo_token,
            payload={"op": "array_merge", "src_array_id": src.id,
                     "dst_array_id": dst.id, "reason": reason, "counts": counts},
            expires_at=now() + timedelta(days=30),
        ))

    return {"ok": True, "src_array_id": src.id, "dst_array_id": dst.id,
            "reason": reason, "counts": counts, "undo_token": undo_token}


# ─────────────────────────────────────────────────────────────────────────────
# DUPLICATE DETECTION + SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _accounts_by_array(db: Session, tenant_id: str) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for ua in db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == tenant_id,
            UtilityAccount.deleted_at.is_(None),
            UtilityAccount.array_id.is_not(None))).scalars().all():
        out.setdefault(ua.array_id, set()).add(ua.account_number)
    return out


def _has_vendor(db: Session, array_id: int) -> bool:
    return (db.execute(select(func.count(InverterConnection.id)).where(
        InverterConnection.array_id == array_id)).scalar() or 0) > 0


def find_duplicate_pairs(db: Session, tenant_id: str) -> list[dict]:
    """Score same-tenant array pairs as duplicate candidates.

    Confidence tiers (drives auto-merge policy in the sweep job):
      STRONG  — share a utility account, OR exact normalized-name match, OR same
                NEPOOL-GIS id. Deterministic; safe to auto-merge.
      MEDIUM  — one normalized name fully CONTAINS the other AND they do NOT
                differ only by a sub-array token AND at least one side is a
                cross-source pair (one vendor + one GMP/bare). Guarded auto-merge.
      WEAK    — partial overlap / same client similar name. Suggestion only.

    Sub-array guard: pairs differing only by a directional/positional token are
    DROPPED entirely (real distinct sub-meters, never duplicates).

    Returns a list of {a_id,b_id,a_name,b_name,confidence,score,reasons,
    cross_source} sorted strongest first. Each unordered pair appears once.
    """
    arrays = db.execute(select(Array).where(
        Array.tenant_id == tenant_id, Array.deleted_at.is_(None))).scalars().all()
    if len(arrays) < 2:
        return []
    uas = _accounts_by_array(db, tenant_id)
    vendor = {a.id: _has_vendor(db, a.id) for a in arrays}

    dismissed: set[tuple[int, int]] = set()
    for d in db.execute(select(ArrayMergeDismissal).where(
            ArrayMergeDismissal.tenant_id == tenant_id)).scalars().all():
        dismissed.add((d.array_a_id, d.array_b_id))

    out: list[dict] = []
    for i in range(len(arrays)):
        for j in range(i + 1, len(arrays)):
            A, B = arrays[i], arrays[j]
            key = tuple(sorted((A.id, B.id)))
            if key in dismissed:
                continue
            nA, nB = _norm_name(A.name), _norm_name(B.name)
            if not nA or not nB:
                continue

            # HARD GUARD: distinct sub-meters of one site → never a duplicate.
            if _differs_only_by_subarray_token(A.name, B.name):
                continue

            score = 0
            reasons: list[str] = []
            # NOTE: within a tenant, uq_account_per_tenant makes (provider,
            # account_number) unique, so two arrays can't actually share a UA —
            # this branch is defensive (kept in case the constraint ever relaxes).
            shared_ua = uas.get(A.id, set()) & uas.get(B.id, set())
            if shared_ua:
                score += 100
                reasons.append(f"shared utility account {next(iter(shared_ua))}")
            exact_name = nA == nB
            if exact_name:
                score += 70
                reasons.append("identical name")
            nepoolA = (A.nepool_gis_id or "").strip()
            nepoolB = (B.nepool_gis_id or "").strip()
            if nepoolA and nepoolB and nepoolA == nepoolB:
                score += 80
                reasons.append(f"same NEPOOL-GIS {nepoolA}")
            containment = (not exact_name) and (nA in nB or nB in nA)
            cross_source = vendor[A.id] != vendor[B.id]
            if containment:
                score += 35
                reasons.append("one name contains the other")

            # Tier assignment
            confidence = None
            if shared_ua or exact_name or (nepoolA and nepoolA == nepoolB):
                confidence = "STRONG"
            elif containment and cross_source:
                # MEDIUM: guarded — already passed the sub-array token guard above.
                confidence = "MEDIUM"
            elif (A.client_id is not None and A.client_id == B.client_id
                  and (nA in nB or nB in nA)):
                confidence = "WEAK"
                score += 15
                reasons.append("same client, overlapping name")

            if confidence is None or score < 30:
                continue

            # Normalize merge direction (which survives).
            dst, src = _preferred_destination(db, A, B)
            out.append({
                "src_id": src.id, "src_name": src.name,
                "dst_id": dst.id, "dst_name": dst.name,
                "a_id": A.id, "b_id": B.id,
                "confidence": confidence, "score": score,
                "reasons": reasons, "cross_source": cross_source,
            })
    out.sort(key=lambda d: -d["score"])
    return out
