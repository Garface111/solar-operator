"""SolarEdge daily-generation pull job.

Fetches the last N days of daily kWh from the SolarEdge Monitoring API
for one array and upserts into the DailyGeneration table.

Idempotency: upsert by (array_id, day). Re-running with the same range
updates existing rows; it never inserts duplicates.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select

from ..adapters.solaredge import fetch_daily_energy, SolarEdgeError
from ..db import SessionLocal
from ..models import Array, DailyGeneration, now

log = logging.getLogger(__name__)


def pull_daily_for_array(
    db,
    array_id: int,
    days_back: int = 90,
) -> dict:
    """Fetch the last N days of daily generation for one array.

    Upserts into DailyGeneration with source='solaredge'.
    Returns {'days_pulled': N, 'days_skipped_zero': K, 'errors': [...]}.

    Caller must provide an open db session.
    """
    arr = db.get(Array, array_id)
    if arr is None:
        return {"days_pulled": 0, "days_skipped_zero": 0, "errors": [f"Array {array_id} not found"]}

    if not arr.solaredge_api_key or not arr.solaredge_site_id:
        return {"days_pulled": 0, "days_skipped_zero": 0, "errors": ["Array has no SolarEdge credentials"]}

    today = date.today()
    start = today - timedelta(days=days_back)
    end = today

    try:
        entries = fetch_daily_energy(arr.solaredge_api_key, arr.solaredge_site_id, start, end)
    except SolarEdgeError as exc:
        return {"days_pulled": 0, "days_skipped_zero": 0, "errors": [str(exc)]}

    days_zero = max(0, days_back + 1 - len(entries))

    if not entries:
        return {"days_pulled": 0, "days_skipped_zero": days_zero, "errors": []}

    days_in_range = [e["day"] for e in entries]
    existing = db.execute(
        select(DailyGeneration).where(
            DailyGeneration.array_id == array_id,
            DailyGeneration.day.in_(days_in_range),
        )
    ).scalars().all()
    existing_by_day: dict[date, DailyGeneration] = {r.day: r for r in existing}

    for entry in entries:
        day, kwh = entry["day"], entry["kwh"]
        if day in existing_by_day:
            existing_by_day[day].kwh = kwh
            existing_by_day[day].source = "solaredge"
            existing_by_day[day].uploaded_at = now()
        else:
            db.add(DailyGeneration(
                tenant_id=arr.tenant_id,
                array_id=array_id,
                day=day,
                kwh=kwh,
                source="solaredge",
            ))

    db.commit()

    return {
        "days_pulled": len(entries),
        "days_skipped_zero": days_zero,
        "errors": [],
    }


def pull_all_solaredge_arrays(days_back: int = 90) -> dict:
    """Pull daily generation for every array with a SolarEdge API key.

    Called by the scheduler at 03:00 UTC daily. Errors per array are logged
    but do not crash the scheduler.
    """
    results: list[dict] = []

    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(
                Array.solaredge_api_key.isnot(None),
                Array.deleted_at.is_(None),
            )
        ).scalars().all()

        for arr in arrays:
            try:
                r = pull_daily_for_array(db, arr.id, days_back=days_back)
                if r["errors"]:
                    log.warning(
                        "solaredge_pull array=%d errors=%s", arr.id, r["errors"]
                    )
                results.append({"array_id": arr.id, **r})
            except Exception as exc:
                log.error("solaredge_pull unhandled error array=%d: %s", arr.id, exc)
                results.append({
                    "array_id": arr.id,
                    "days_pulled": 0,
                    "days_skipped_zero": 0,
                    "errors": [str(exc)],
                })

    return {"arrays_processed": len(results), "results": results}
