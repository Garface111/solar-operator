"""
NEPOOL Operator — AI-assisted NEPOOL ID assignment from spreadsheet.

Unlike /v1/ingest/* (which creates new clients/arrays), this path ONLY assigns
NEPOOL-GIS IDs to arrays that already exist in the tenant's account. It never
creates or renames anything.

Endpoints:
  GET  /v1/account/nepool/stats    → {arrays_missing_nepool: int}
  POST /v1/account/nepool/preview  → proposals + confidence scores (NOT saved)
  POST /v1/account/nepool/commit   → assigns nepool_gis_id to confirmed arrays
"""
from __future__ import annotations

import io
import os
import logging
import re
from difflib import SequenceMatcher
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .db import SessionLocal
from .models import Array, Client
from .account import tenant_from_session, require_not_demo
from .ingest import (
    _file_to_text,
    _detect_gmcs_shape,
    _extract_from_gmcs,
    _llm_extract_hierarchical,
    _flatten_hierarchy_to_pairs,
    EXTRACTION_PROMPT,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Minimum fuzzy score to include a pair in proposals at all.
# High (>=0.95): checked by default; Likely (0.85-0.95): checked; Possible (0.70-0.85): unchecked.
FUZZY_INCLUDE_THRESHOLD = 0.70

# NEPOOL extraction reuses the same hierarchical prompt as ingest so the LLM
# scans across all sheets — NEPOOL IDs may live on a different sheet than array names.
NEPOOL_EXTRACTION_PROMPT = EXTRACTION_PROMPT

# Regex fallback: matches "Array Name (12345)" or "Array Name: 12345" etc.
_NEPOOL_INLINE_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 &',\.\-]{2,60}?)\s*[\(:\-–]\s*(\d{4,6})\s*[\)\:]?"
)


# ─── schemas ──────────────────────────────────────────────────────────────

class Assignment(BaseModel):
    array_id: int
    nepool_gis_id: str


class CommitBody(BaseModel):
    assignments: list[Assignment]


# ─── GMCS pair extraction ──────────────────────────────────────────────────

def _extract_pairs_from_gmcs(wb) -> list[dict]:
    """Extract (array_name, nepool_gis_id) pairs from a GMCS-format workbook."""
    if not _detect_gmcs_shape(wb):
        return []
    rows = _extract_from_gmcs(wb)
    return [
        {"array_name": r["array_name"], "nepool_gis_id": r["nepool_gis_id"]}
        for r in rows
        if r.get("array_name") and r.get("nepool_gis_id")
    ]


# ─── LLM pair extraction ───────────────────────────────────────────────────

def _llm_extract_pairs(text: str) -> Optional[list[dict]]:
    """Use the shared hierarchical extraction then flatten to (name, id) pairs.

    This lets the LLM consider NEPOOL IDs across ALL sheets — e.g. array names
    on sheet 1 and NEPOOL IDs in a separate column on sheet 2 are joined by
    array name inside the hierarchical response."""
    hierarchy = _llm_extract_hierarchical(text)
    if hierarchy is None:
        return None
    return _flatten_hierarchy_to_pairs(hierarchy)


# ─── heuristic fallback ────────────────────────────────────────────────────

def _heuristic_extract_pairs(text: str) -> list[dict]:
    """Scan text for patterns like 'Array Name (12345)' or 'Name: 12345'."""
    pairs: list[dict] = []
    seen_ids: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NEPOOL_INLINE_RE.search(line)
        if m:
            name = m.group(1).strip().rstrip(",- ").strip()
            gis_id = m.group(2)
            if gis_id not in seen_ids and name and len(name) > 2:
                pairs.append({"array_name": name, "nepool_gis_id": gis_id})
                seen_ids.add(gis_id)
    return pairs


# ─── fuzzy matching ────────────────────────────────────────────────────────

def _fuzzy_score(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _best_match(
    extracted_name: str, arrays: list[Array]
) -> tuple[Optional[Array], float]:
    best: Optional[Array] = None
    best_score = 0.0
    for arr in arrays:
        score = _fuzzy_score(extracted_name, arr.name)
        if score > best_score:
            best_score = score
            best = arr
    if best_score >= FUZZY_INCLUDE_THRESHOLD:
        return best, best_score
    return None, best_score


# ─── endpoints ────────────────────────────────────────────────────────────

@router.get("/v1/account/nepool/stats")
def nepool_stats(authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        missing = db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.nepool_gis_id.is_(None),
            )
        ).scalars().all()
    return {"arrays_missing_nepool": len(missing)}


@router.post("/v1/account/nepool/preview")
async def nepool_preview(
    file: UploadFile = File(...),
    client_id: Optional[int] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Parse an uploaded file, extract (array_name, nepool_gis_id) pairs,
    and fuzzy-match each to existing arrays. Nothing is written here.

    When client_id is provided, matching is scoped to that client's arrays only."""
    t = tenant_from_session(authorization)

    if client_id is not None:
        with SessionLocal() as db:
            client = db.execute(
                select(Client).where(
                    Client.id == client_id,
                    Client.tenant_id == t.id,
                    Client.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        if client is None:
            raise HTTPException(404, f"Client {client_id} not found")

    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")

    source = "llm"
    pairs: Optional[list[dict]] = None

    fname = (file.filename or "").lower()
    if fname.endswith(".xlsx"):
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            try:
                gmcs_pairs = _extract_pairs_from_gmcs(wb)
                if gmcs_pairs:
                    pairs = gmcs_pairs
                    source = "gmcs_shape"
            finally:
                wb.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, f"Couldn't open that Excel file: {exc}") from exc

    if pairs is None:
        text = _file_to_text(file.filename or "", data)
        if not text.strip():
            raise HTTPException(400, "Couldn't read any content from that file")
        llm_pairs = _llm_extract_pairs(text)
        if llm_pairs is not None:
            pairs = llm_pairs
            source = "llm"
        else:
            pairs = _heuristic_extract_pairs(text)
            source = "heuristic"

    # Normalize and deduplicate extracted pairs.
    clean_pairs: list[dict] = []
    seen: set[str] = set()
    for p in (pairs or []):
        name_val = (p.get("array_name") or "").strip()
        gis_val = re.sub(r"\D", "", (p.get("nepool_gis_id") or "").strip())
        if not name_val or not gis_val:
            continue
        key = f"{name_val.lower()}|{gis_val}"
        if key in seen:
            continue
        seen.add(key)
        clean_pairs.append({"array_name": name_val, "nepool_gis_id": gis_val})

    # Load tenant's existing arrays (eager-load client for display names).
    # When client_id is set, scope to that client's arrays only.
    with SessionLocal() as db:
        array_filters = [Array.tenant_id == t.id, Array.deleted_at.is_(None)]
        if client_id is not None:
            array_filters.append(Array.client_id == client_id)
        existing_arrays: list[Array] = db.execute(
            select(Array)
            .options(joinedload(Array.client))
            .where(*array_filters)
        ).scalars().all()

    proposals: list[dict] = []
    unmatched_pairs: list[dict] = []
    skipped_overwrites = 0
    matched_array_ids: set[int] = set()

    for p in clean_pairs:
        arr, score = _best_match(p["array_name"], existing_arrays)
        if arr is None:
            unmatched_pairs.append(p)
            continue
        # Skip arrays that already have a NEPOOL ID (overwrite protection).
        if arr.nepool_gis_id is not None:
            skipped_overwrites += 1
            continue
        matched_array_ids.add(arr.id)
        proposals.append({
            "extracted_name": p["array_name"],
            "extracted_nepool_gis_id": p["nepool_gis_id"],
            "match": {
                "array_id": arr.id,
                "array_name": arr.name,
                "current_nepool_gis_id": arr.nepool_gis_id,
                "confidence": round(score, 3),
                "would_overwrite": False,
            },
        })

    # High confidence first, then by extracted name.
    proposals.sort(key=lambda x: (-x["match"]["confidence"], x["extracted_name"]))

    # Arrays available for manual assignment in the frontend dropdown:
    # missing NEPOOL IDs and not already matched above.
    available_arrays = [
        {
            "array_id": arr.id,
            "array_name": arr.name,
            "client_name": arr.client.name if arr.client else None,
        }
        for arr in existing_arrays
        if arr.nepool_gis_id is None and arr.id not in matched_array_ids
    ]
    available_arrays.sort(key=lambda x: x["array_name"])

    return {
        "ok": True,
        "source": source,
        "pairs_extracted": len(clean_pairs),
        "matches_proposed": len(proposals),
        "unmatched": len(unmatched_pairs),
        "skipped_overwrites": skipped_overwrites,
        "proposals": proposals,
        "unmatched_pairs": unmatched_pairs,
        "available_arrays": available_arrays,
    }


@router.post("/v1/account/nepool/commit")
def nepool_commit(
    body: CommitBody,
    authorization: Optional[str] = Header(default=None),
):
    """Assign nepool_gis_id to confirmed arrays. Refuses to overwrite an
    existing non-null NEPOOL ID — clear it per-array first if needed."""
    t = tenant_from_session(authorization)
    require_not_demo(t)

    if not body.assignments:
        raise HTTPException(400, "Nothing to assign")

    updated = 0
    errors: list[dict] = []

    with SessionLocal() as db:
        for assignment in body.assignments:
            gis_val = re.sub(r"\D", "", (assignment.nepool_gis_id or "").strip())
            if not gis_val:
                errors.append({"array_id": assignment.array_id, "reason": "Invalid NEPOOL ID"})
                continue

            arr = db.execute(
                select(Array).where(
                    Array.id == assignment.array_id,
                    Array.tenant_id == t.id,
                    Array.deleted_at.is_(None),
                )
            ).scalar_one_or_none()

            if arr is None:
                errors.append({"array_id": assignment.array_id, "reason": "Array not found"})
                continue

            if arr.nepool_gis_id is not None:
                errors.append({
                    "array_id": assignment.array_id,
                    "reason": (
                        f'Array "{arr.name}" already has a NEPOOL ID; '
                        "clear it first if you want to change."
                    ),
                })
                continue

            arr.nepool_gis_id = gis_val
            updated += 1

        db.commit()

    return {
        "ok": True,
        "updated": updated,
        "errors": errors,
    }
