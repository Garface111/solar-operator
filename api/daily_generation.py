"""
Daily generation CSV ingest + coverage endpoints.

POST /v1/account/arrays/{array_id}/daily-csv
    Multipart upload of a GMP daily-generation CSV. Upserts DailyGeneration
    rows by (array_id, day). Re-uploading the same range overwrites existing
    data — that is the operator's intent ("I just downloaded fresh data").

GET  /v1/account/arrays/{array_id}/daily-coverage
    Returns day count and date range of uploaded DailyGeneration rows.

Auth: dashboard session token (Bearer, same as /v1/account/* endpoints).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, UploadFile, File
from sqlalchemy import select, func

from .account import tenant_from_session, require_not_demo, require_not_demo
from .db import SessionLocal
from .models import Array, DailyGeneration, now

log = logging.getLogger(__name__)

router = APIRouter()

# ── date / value parsing helpers ──────────────────────────────────────────────

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")


def _parse_date(s: str) -> Optional[date]:
    s = s.strip().strip("\"'")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_kwh(s: str) -> Optional[float]:
    s = s.strip().strip("\"'").replace(",", "")
    try:
        v = float(s)
        return v if v >= 0 else None  # negative kWh skipped per spec
    except ValueError:
        return None


# ── CSV format detection ───────────────────────────────────────────────────────

_DATE_KEYWORDS = ("date", "day")
_GEN_KEYWORDS = ("kwh generated", "generation", "production", "solar", "net meter", "kwh_generated")


def _is_date_header(cell: str) -> bool:
    h = cell.strip().strip("\"'").lower()
    return any(k in h for k in _DATE_KEYWORDS)


def _is_gen_header(cell: str) -> bool:
    h = cell.strip().strip("\"'").lower()
    return any(k in h for k in _GEN_KEYWORDS)


def _detect_columns(rows: list[list[str]]) -> Optional[tuple[int, int, int]]:
    """Return (header_row_idx, date_col_idx, kwh_col_idx) or None."""
    for i, row in enumerate(rows[:10]):
        if len(row) < 2:
            continue
        date_col = next((j for j, v in enumerate(row) if _is_date_header(v)), None)
        gen_col = next((j for j, v in enumerate(row) if _is_gen_header(v)), None)
        if date_col is not None and gen_col is not None and date_col != gen_col:
            return i, date_col, gen_col
    return None


# ── main ingest logic ──────────────────────────────────────────────────────────

def _parse_csv_rows(
    content: bytes,
) -> tuple[list[tuple[date, float]], int, str]:
    """Parse CSV bytes into (day, kwh) pairs.

    Returns (parsed_rows, rows_skipped, detected_format).
    Raises HTTPException(400) if the file is unreadable or has no data rows.
    """
    try:
        text = content.decode("utf-8-sig")  # strip BOM if present
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)

    if not all_rows:
        raise HTTPException(400, "Empty CSV — no rows found")

    detected = _detect_columns(all_rows)

    if detected is not None:
        header_idx, date_col, kwh_col = detected
        data_rows = all_rows[header_idx + 1 :]
        fmt = "header-detected"
    else:
        # Format 3 fallback: try treating row 0 as data (no header)
        if len(all_rows[0]) >= 2 and _parse_date(all_rows[0][0]) and _parse_kwh(all_rows[0][1]) is not None:
            data_rows = all_rows
            date_col, kwh_col = 0, 1
            fmt = "no-header-fallback"
        else:
            # Could not parse — return informative 400 with first 3 rows
            sample = all_rows[:3]
            raise HTTPException(
                400,
                f"Could not detect CSV format. First 3 rows: {sample}",
            )

    parsed: list[tuple[date, float]] = []
    skipped = 0
    for row in data_rows:
        if not row or all(c.strip() == "" for c in row):
            continue  # blank line
        if len(row) <= max(date_col, kwh_col):
            skipped += 1
            continue
        d = _parse_date(row[date_col])
        k = _parse_kwh(row[kwh_col])
        if d is None or k is None:
            skipped += 1
            continue
        parsed.append((d, k))

    if not parsed:
        raise HTTPException(400, "No data rows found — all rows were skipped or unparseable")

    return parsed, skipped, fmt


# ── endpoints ──────────────────────────────────────────────────────────────────

@router.post("/v1/account/arrays/{array_id}/daily-csv")
async def upload_daily_csv(
    array_id: int,
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """Accept a GMP daily generation CSV and upsert DailyGeneration rows."""
    tenant = tenant_from_session(authorization)
    require_not_demo(tenant)

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id:
            raise HTTPException(404, "Array not found")

        content = await file.read()
        if not content:
            raise HTTPException(400, "Uploaded file is empty")

        parsed_rows, rows_skipped, _fmt = _parse_csv_rows(content)

        # Bulk-fetch existing rows so we can distinguish insert vs update
        days = [d for d, _ in parsed_rows]
        existing = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == array_id,
                DailyGeneration.day.in_(days),
            )
        ).scalars().all()
        existing_by_day: dict[date, DailyGeneration] = {r.day: r for r in existing}

        inserted = 0
        updated = 0
        for day, kwh in parsed_rows:
            if day in existing_by_day:
                existing_by_day[day].kwh = kwh
                existing_by_day[day].source = "csv"
                existing_by_day[day].uploaded_at = now()
                updated += 1
            else:
                db.add(DailyGeneration(
                    tenant_id=tenant.id,
                    array_id=array_id,
                    day=day,
                    kwh=kwh,
                    source="csv",
                ))
                inserted += 1

        db.commit()

    all_days = sorted(d for d, _ in parsed_rows)
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": rows_skipped,
        "date_range": {
            "start": all_days[0].isoformat(),
            "end": all_days[-1].isoformat(),
        },
        "source": "csv",
    }


@router.get("/v1/account/arrays/{array_id}/daily-coverage")
def get_daily_coverage(
    array_id: int,
    authorization: str | None = Header(default=None),
):
    """Return day count and date range of uploaded DailyGeneration rows."""
    tenant = tenant_from_session(authorization)

    with SessionLocal() as db:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant.id:
            raise HTTPException(404, "Array not found")

        rows = db.execute(
            select(DailyGeneration.day, DailyGeneration.source).where(
                DailyGeneration.array_id == array_id,
            )
        ).all()

    if not rows:
        return {
            "day_count": 0,
            "first_day": None,
            "last_day": None,
            "source_counts": {},
        }

    source_counts: dict[str, int] = {}
    for _, src in rows:
        source_counts[src] = source_counts.get(src, 0) + 1

    all_days = sorted(d for d, _ in rows)
    return {
        "day_count": len(all_days),
        "first_day": all_days[0].isoformat(),
        "last_day": all_days[-1].isoformat(),
        "source_counts": source_counts,
    }
