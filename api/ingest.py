"""
Solar Operator — AI spreadsheet ingest (Mega-Vector V4).

A NEPOOL stamping-agent tenant already keeps a master roster of every operator
they report for: operator name, array name, NEPOOL-GIS ID, sometimes a GMP
account number. Hand-entering 50 arrays is a non-starter. This module collapses
that to: drop the spreadsheet → confirm a preview table → one-click commit.

Two endpoints (both auth'd as the calling tenant):

  POST /v1/ingest/preview   multipart .xlsx/.xls/.csv  →  parsed rows (NOT saved)
  POST /v1/ingest/commit    edited JSON rows           →  find_or_create + summary

Parsing strategy: flatten the sheet to plain text, hand it to a cheap LLM
(Anthropic Haiku preferred, OpenAI fallback) with a strict-JSON extraction
prompt. If no LLM key is configured OR the call fails, we fall back to a
column-name heuristic parser so the feature never hard-fails.

NOTE (deliberate deviation from the V4 brief): we do NOT add pandas. A CSV is
already plain text and openpyxl already covers .xlsx, so pandas would add a
heavy numpy-backed dependency to the Railway image for zero benefit here. CSVs
are parsed with the stdlib `csv` module.
"""
from __future__ import annotations

import io
import os
import csv
import json
import logging
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select

from .db import SessionLocal
from .models import Client, Array, UtilityAccount
from .account import tenant_from_session

logger = logging.getLogger(__name__)

router = APIRouter()

# Cap how much text we send to the LLM — a 50-array roster is a few KB; this
# guards against someone dropping a 10k-row export and running up a bill.
MAX_TEXT_CHARS = 60_000
# Cap rows committed in one shot (preview can show more, but refuse a runaway).
MAX_COMMIT_ROWS = 500

ANTHROPIC_MODEL = os.getenv("INGEST_LLM_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.getenv("INGEST_OPENAI_MODEL", "gpt-4o-mini")

EXTRACTION_PROMPT = (
    "You are given a tabular roster from a NEPOOL-GIS reporting consultant. "
    "Each row likely represents one solar array. Extract every array you can "
    "find. For each, return JSON with: operator_name (the company/person who "
    "owns the array — this maps to a Client), array_name (the name of the "
    "physical installation), nepool_gis_id (the 5-digit numeric ID, may be "
    "labeled NEPOOL, NEPOOL-GIS, GIS ID, Asset ID, etc), gmp_account_number "
    "(if present), notes (any free-text the row has). Return strictly: "
    '{"arrays": [{...}, ...]}. If a column is unclear or missing, set the '
    "field to null. Skip rows that are obviously headers or totals."
)

FIELDS = ("operator_name", "array_name", "nepool_gis_id", "gmp_account_number", "notes")


# ─── schemas ──────────────────────────────────────────────────────────────

class IngestRow(BaseModel):
    operator_name: Optional[str] = None
    array_name: Optional[str] = None
    nepool_gis_id: Optional[str] = None
    gmp_account_number: Optional[str] = None
    notes: Optional[str] = None


class CommitBody(BaseModel):
    arrays: list[IngestRow]


# ─── file → plain text ─────────────────────────────────────────────────────

def _xlsx_to_text(data: bytes) -> str:
    """Flatten the FIRST worksheet to tab-separated rows."""
    from openpyxl import load_workbook  # local import: heavy, only needed here

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        lines: list[str] = []
        for row in ws.iter_rows(values_only=True):
            if row is None:
                continue
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                lines.append("\t".join(cells))
        return "\n".join(lines)
    finally:
        wb.close()


def _csv_to_text(data: bytes) -> str:
    """Normalize a CSV to tab-separated rows (CSV is already text)."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    lines = ["\t".join(cell.strip() for cell in row) for row in reader if any(c.strip() for c in row)]
    return "\n".join(lines)


def _file_to_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = _csv_to_text(data)
    elif name.endswith(".xlsx") or name.endswith(".xls"):
        # .xls (legacy BIFF) isn't supported by openpyxl; surface a clear error
        # rather than a cryptic parse failure.
        if name.endswith(".xls"):
            try:
                text = _xlsx_to_text(data)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(
                    400,
                    "Legacy .xls files aren't supported — re-save as .xlsx or .csv "
                    "and try again.",
                ) from e
        else:
            text = _xlsx_to_text(data)
    else:
        raise HTTPException(400, "Upload a .xlsx or .csv file")
    return text[:MAX_TEXT_CHARS]


# ─── LLM extraction ────────────────────────────────────────────────────────

def _extract_json_block(raw: str) -> dict:
    """Pull the first {...} JSON object out of an LLM response, tolerating
    markdown fences or stray prose around it."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(raw[start : end + 1])
    raise ValueError("no JSON object in LLM response")


def _call_anthropic(text: str, api_key: str) -> list[dict]:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": f"{EXTRACTION_PROMPT}\n\nHere is the roster:\n\n{text}",
                },
                # Prefill forces the model straight into JSON.
                {"role": "assistant", "content": '{"arrays":'},
            ],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    content = "".join(b.get("text", "") for b in body.get("content", []))
    parsed = _extract_json_block('{"arrays":' + content)
    return parsed.get("arrays", [])


def _call_openai(text: str, api_key: str) -> list[dict]:
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        json={
            "model": OPENAI_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": f"Here is the roster:\n\n{text}"},
            ],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    return _extract_json_block(content).get("arrays", [])


def _llm_extract(text: str) -> Optional[list[dict]]:
    """Try Anthropic, then OpenAI. Return None if no key configured or both
    fail — the caller falls back to the heuristic parser."""
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    if anthropic_key:
        try:
            return _call_anthropic(text, anthropic_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("Anthropic ingest extraction failed: %s", e)
    if openai_key:
        try:
            return _call_openai(text, openai_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("OpenAI ingest extraction failed: %s", e)
    return None


# ─── heuristic fallback parser ─────────────────────────────────────────────

# Column-header keyword → our canonical field. First match wins per column.
_HEURISTIC_MAP = [
    ("nepool_gis_id", ("nepool", "gis", "asset id", "asset_id")),
    ("gmp_account_number", ("gmp", "account", "acct")),
    ("operator_name", ("operator", "owner", "company", "client", "customer")),
    ("array_name", ("array", "installation", "site", "system", "project", "name")),
    ("notes", ("note", "comment", "remark")),
]


def _heuristic_extract(text: str) -> list[dict]:
    """Best-effort parse when no LLM is available: match column headers to
    fields by keyword, then read each row into that mapping."""
    rows = [ln.split("\t") for ln in text.splitlines() if ln.strip()]
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    # Map each column index → canonical field.
    col_field: dict[int, str] = {}
    used: set[str] = set()
    for idx, col in enumerate(header):
        for field, keywords in _HEURISTIC_MAP:
            if field in used:
                continue
            if any(k in col for k in keywords):
                col_field[idx] = field
                used.add(field)
                break

    out: list[dict] = []
    for raw in rows[1:]:
        entry = {f: None for f in FIELDS}
        any_value = False
        for idx, val in enumerate(raw):
            field = col_field.get(idx)
            v = val.strip()
            if field and v:
                entry[field] = v
                any_value = True
        # If headers were unrecognizable, fall back to positional guessing:
        # operator, array, nepool — the most common roster shape.
        if not col_field and len(raw) >= 2:
            entry["operator_name"] = (raw[0] or "").strip() or None
            entry["array_name"] = (raw[1] or "").strip() or None
            if len(raw) >= 3:
                m = re.search(r"\b\d{4,6}\b", raw[2])
                entry["nepool_gis_id"] = m.group(0) if m else (raw[2].strip() or None)
            any_value = any(entry.values())
        if any_value:
            out.append(entry)
    return out


def _normalize(rows: list[dict]) -> list[dict]:
    """Coerce LLM/heuristic output into clean {FIELDS} dicts."""
    clean: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        entry = {}
        for f in FIELDS:
            v = r.get(f)
            if v is None:
                entry[f] = None
            else:
                s = str(v).strip()
                entry[f] = s or None
        # Drop rows with nothing meaningful (no operator AND no array).
        if entry["operator_name"] or entry["array_name"]:
            clean.append(entry)
    return clean


# ─── endpoints ─────────────────────────────────────────────────────────────

@router.post("/v1/ingest/preview")
async def ingest_preview(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(default=None),
):
    """Parse an uploaded roster and return extracted rows AS A PREVIEW.
    Nothing is written to the database here."""
    tenant_from_session(authorization)  # auth gate

    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")

    text = _file_to_text(file.filename or "", data)
    if not text.strip():
        raise HTTPException(400, "Couldn't read any rows from that file")

    rows = _llm_extract(text)
    source = "llm"
    if rows is None:
        rows = _heuristic_extract(text)
        source = "heuristic"

    arrays = _normalize(rows)
    return {
        "ok": True,
        "source": source,
        "count": len(arrays),
        "arrays": arrays,
    }


@router.post("/v1/ingest/commit")
def ingest_commit(
    body: CommitBody,
    authorization: Optional[str] = Header(default=None),
):
    """Persist the (user-confirmed, possibly edited) rows. For each row:
      find_or_create Client by operator_name → Array by name → UtilityAccount.
    Returns counts of newly created records."""
    t = tenant_from_session(authorization)

    rows = _normalize([r.model_dump() for r in body.arrays])
    if not rows:
        raise HTTPException(400, "Nothing to import")
    if len(rows) > MAX_COMMIT_ROWS:
        raise HTTPException(
            400, f"Too many rows ({len(rows)}). Import at most {MAX_COMMIT_ROWS} at a time."
        )

    clients_created = arrays_created = accounts_created = 0
    # Within-batch caches so two rows for the same operator don't both try to
    # create the client (and so we don't re-query each row).
    client_cache: dict[str, Client] = {}

    with SessionLocal() as db:
        def find_or_create_client(name: str) -> Client:
            nonlocal clients_created
            key = name.lower()
            if key in client_cache:
                return client_cache[key]
            c = db.execute(
                select(Client).where(Client.tenant_id == t.id, Client.name == name)
            ).scalar_one_or_none()
            if c is None:
                c = Client(tenant_id=t.id, name=name, active=True)
                db.add(c)
                db.flush()
                clients_created += 1
            client_cache[key] = c
            return c

        for r in rows:
            operator = r["operator_name"] or "Unassigned"
            array_name = r["array_name"]
            if not array_name:
                # No array name → nothing concrete to create; skip.
                continue

            client = find_or_create_client(operator)

            # Array unique on (tenant_id, name). If it already exists, reuse it
            # and backfill NEPOOL / notes if they were blank.
            arr = db.execute(
                select(Array).where(Array.tenant_id == t.id, Array.name == array_name)
            ).scalar_one_or_none()
            if arr is None:
                arr = Array(
                    tenant_id=t.id,
                    client_id=client.id,
                    name=array_name,
                    nepool_gis_id=r["nepool_gis_id"],
                    notes=r["notes"],
                )
                db.add(arr)
                db.flush()
                arrays_created += 1
            else:
                if not arr.client_id:
                    arr.client_id = client.id
                if not arr.nepool_gis_id and r["nepool_gis_id"]:
                    arr.nepool_gis_id = r["nepool_gis_id"]
                if not arr.notes and r["notes"]:
                    arr.notes = r["notes"]

            acct_num = (r["gmp_account_number"] or "").strip()
            if acct_num:
                # UtilityAccount unique on (tenant_id, provider, account_number).
                existing = db.execute(
                    select(UtilityAccount).where(
                        UtilityAccount.tenant_id == t.id,
                        UtilityAccount.provider == "gmp",
                        UtilityAccount.account_number == acct_num,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    db.add(UtilityAccount(
                        tenant_id=t.id,
                        array_id=arr.id,
                        provider="gmp",
                        account_number=acct_num,
                        nickname=array_name,
                    ))
                    accounts_created += 1
                elif existing.array_id is None:
                    existing.array_id = arr.id

        db.commit()

    return {
        "ok": True,
        "clients_created": clients_created,
        "arrays_created": arrays_created,
        "accounts_created": accounts_created,
    }
