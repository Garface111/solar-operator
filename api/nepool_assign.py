"""
Solar Operator — AI-assisted NEPOOL ID assignment from spreadsheet.

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
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .db import SessionLocal
from .models import Array
from .account import tenant_from_session
from .ingest import (
    _file_to_text,
    _extract_json_block,
    _detect_gmcs_shape,
    _extract_from_gmcs,
)

logger = logging.getLogger(__name__)

router = APIRouter()

ANTHROPIC_MODEL = os.getenv("INGEST_LLM_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.getenv("INGEST_OPENAI_MODEL", "gpt-4o-mini")
MAX_TEXT_CHARS = 60_000

# Minimum fuzzy score to include a pair in proposals at all.
# High (>=0.95): checked by default; Likely (0.85-0.95): checked; Possible (0.70-0.85): unchecked.
FUZZY_INCLUDE_THRESHOLD = 0.70

NEPOOL_EXTRACTION_PROMPT = (
    "You are looking at a Vermont solar operator's records. Find every pair of "
    "(array_name, nepool_gis_id) you can. The NEPOOL-GIS ID is a 5-digit "
    "numeric (sometimes 4-6 digits) often labeled NEPOOL, NEPOOL-GIS, GIS, Asset ID, "
    "or appearing in parentheses next to the array name like 'Tannery Brook (12345)'. "
    "Return strictly: {\"pairs\": [{\"array_name\": \"...\", \"nepool_gis_id\": \"...\"}, ...]}. "
    "Skip pairs where either field is missing. Skip totals/headers/footnotes."
)

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

def _call_anthropic_pairs(text: str, api_key: str) -> list[dict]:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "user",
                    "content": f"{NEPOOL_EXTRACTION_PROMPT}\n\nHere is the document:\n\n{text}",
                },
                {"role": "assistant", "content": '{"pairs":'},
            ],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    content = "".join(b.get("text", "") for b in body.get("content", []))
    parsed = _extract_json_block('{"pairs":' + content)
    return parsed.get("pairs", [])


def _call_openai_pairs(text: str, api_key: str) -> list[dict]:
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        json={
            "model": OPENAI_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": NEPOOL_EXTRACTION_PROMPT},
                {"role": "user", "content": f"Here is the document:\n\n{text}"},
            ],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    return _extract_json_block(content).get("pairs", [])


def _llm_extract_pairs(text: str) -> Optional[list[dict]]:
    """Try Anthropic, then OpenAI. Return None if no key or both fail."""
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    if anthropic_key:
        try:
            return _call_anthropic_pairs(text, anthropic_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("Anthropic nepool extraction failed: %s", e)
    if openai_key:
        try:
            return _call_openai_pairs(text, openai_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("OpenAI nepool extraction failed: %s", e)
    return None


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
    authorization: Optional[str] = Header(default=None),
):
    """Parse an uploaded file, extract (array_name, nepool_gis_id) pairs,
    and fuzzy-match each to existing arrays. Nothing is written here."""
    t = tenant_from_session(authorization)

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
    with SessionLocal() as db:
        existing_arrays: list[Array] = db.execute(
            select(Array)
            .options(joinedload(Array.client))
            .where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
            )
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
