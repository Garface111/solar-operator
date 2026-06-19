"""Self-healing deep-history backfill for inverter connections.

THE PROBLEM IT SOLVES
  The nightly inverter pull (jobs/inverter_pull) only reaches ~90 days back. A
  freshly-connected SolarEdge/Fronius/SMA/Locus array therefore shows just the
  CURRENT year in the Trends tab — its real multi-year history sits on the
  vendor's servers, never pulled. (This is exactly why the demo account showed
  only 2026 until a manual backfill.)

THE FIX (self-healing, two triggers)
  • ON CONNECT: _connect_inverter fires backfill_connection_history in a
    background thread, so a new customer's full history lands within minutes.
  • SCHEDULED HEAL: heal_missing_history() runs periodically and backfills any
    connection whose history_backfilled_at is still NULL (covers connects that
    raced/failed, pre-existing connections, and transient vendor outages — it
    only STAMPS on success, so a failed attempt is retried next run).

  Vendor-agnostic: uses inverters.fetch_daily(vendor, config, start, end) for any
  vendor with SUPPORTS_DAILY. Chunks year-by-year (1-year max span is the safe
  common denominator — SolarEdge caps DAY energy at ~1 year/request). Upserts
  into DailyGeneration keyed (array_id, day); NEVER clobbers a non-vendor real
  source (csv/manual/utility_meter/gmp_api), only fills gaps + refreshes its own
  rows and bill_prorate placeholders. Idempotent.

  HISTORY_START_YEAR caps how far back we probe (SolarEdge sites rarely predate
  ~2010; empty years just return nothing and cost one cheap request).
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import inverters
from ..db import SessionLocal
from ..inverters import InverterError
from ..models import Array, DailyGeneration, InverterConnection, now

log = logging.getLogger(__name__)

HISTORY_START_YEAR = 2010
# Real metered sources from OTHER feeds we must never overwrite with a vendor
# history pull. (The vendor's own source string is allowed to refresh itself.)
_PROTECT_SOURCES = {
    "csv", "manual", "utility_meter", "gmp_api", "gmp_portal_scrape", "smarthub",
}


def _legacy_solaredge_conns(db: Session) -> list[InverterConnection]:
    """Synthesize InverterConnection-shaped rows for legacy arrays that carry
    SolarEdge creds on the Array columns but have no real connection row.
    Returns transient (un-added) InverterConnection objects we can stamp via the
    array's real row if one is later created; for healing we operate on the real
    rows only, so legacy arrays are handled by ensuring a row exists elsewhere.
    Here we just skip them (they're covered by the nightly virtual-connection
    pull + a real row once the owner reconnects)."""
    return []


def backfill_connection_history(
    conn_id: int, *, start_year: int = HISTORY_START_YEAR,
    db: Session | None = None,
) -> dict:
    """Pull the FULL multi-year daily history for ONE inverter connection and
    upsert it into DailyGeneration. Stamps history_backfilled_at ONLY on success.

    Returns {connection_id, array_id, vendor, inserted, updated, first_day,
             years_probed, stamped, error?}.
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        conn = db.get(InverterConnection, conn_id)
        if conn is None:
            return {"connection_id": conn_id, "error": "connection not found"}
        arr = db.get(Array, conn.array_id)
        if arr is None or arr.deleted_at is not None:
            # Orphaned/soft-deleted array — there's no live array to show history
            # for, so STAMP it done. Otherwise the healer would retry this dead
            # connection on every run forever (the bug that left 9 "pending").
            conn.history_backfilled_at = now()
            db.commit()
            return {"connection_id": conn_id, "array_id": conn.array_id,
                    "stamped": True, "note": "array missing/deleted — skipped"}

        vendor = conn.vendor
        module = inverters.VENDORS.get(vendor)
        if module is None or not getattr(module, "SUPPORTS_DAILY", False):
            # Nothing pullable (e.g. chint). Mark done so the healer stops
            # retrying — there's no history API to wait for.
            conn.history_backfilled_at = now()
            db.commit()
            return {"connection_id": conn_id, "array_id": arr.id, "vendor": vendor,
                    "inserted": 0, "updated": 0, "stamped": True,
                    "note": "vendor has no daily history pull"}

        config = dict(conn.config or {})
        today = date.today()
        # preload existing rows once (update-in-place without per-day queries)
        existing = {
            r.day: r for r in db.execute(
                select(DailyGeneration).where(DailyGeneration.array_id == arr.id)
            ).scalars().all()
        }
        ins = upd = 0
        first_day = None
        years_probed = 0
        had_error = False
        for yr in range(start_year, today.year + 1):
            chunk_start = date(yr, 1, 1)
            chunk_end = date(yr, 12, 31) if yr < today.year else today
            years_probed += 1
            try:
                entries = inverters.fetch_daily(vendor, config, chunk_start, chunk_end)
            except InverterError as exc:
                had_error = True
                log.warning("history backfill conn=%s vendor=%s year=%s error=%s",
                            conn_id, vendor, yr, exc)
                continue
            except Exception as exc:  # noqa: BLE001 — one bad year mustn't kill the rest
                had_error = True
                log.error("history backfill conn=%s year=%s unhandled: %s", conn_id, yr, exc)
                continue
            for e in entries:
                d, kwh = e.get("day"), e.get("kwh")
                if d is None or not kwh:
                    continue
                if first_day is None or d < first_day:
                    first_day = d
                row = existing.get(d)
                if row is not None:
                    if row.source in _PROTECT_SOURCES:
                        continue  # a stronger real feed already owns this day
                    if abs((row.kwh or 0) - kwh) > 1e-6 or row.source != vendor:
                        row.kwh = kwh
                        row.source = vendor
                        row.uploaded_at = now()
                        upd += 1
                else:
                    ng = DailyGeneration(tenant_id=arr.tenant_id, array_id=arr.id,
                                         day=d, kwh=kwh, source=vendor)
                    db.add(ng)
                    existing[d] = ng
                    ins += 1

        # Stamp ONLY if no year hit an error — a partial/failed pass stays NULL
        # so the scheduled healer retries it next run (true self-healing).
        stamped = not had_error
        if stamped:
            conn.history_backfilled_at = now()
        db.commit()
        return {"connection_id": conn_id, "array_id": arr.id, "vendor": vendor,
                "inserted": ins, "updated": upd,
                "first_day": str(first_day) if first_day else None,
                "years_probed": years_probed, "stamped": stamped,
                "had_error": had_error}
    finally:
        if own:
            db.close()


def backfill_connection_history_async(conn_id: int) -> None:
    """Fire-and-forget the backfill in a daemon thread (used by the connect
    endpoint so the HTTP response isn't blocked by a multi-year pull)."""
    def _run():
        try:
            r = backfill_connection_history(conn_id)
            log.info("on-connect history backfill conn=%s: %s", conn_id, r)
        except Exception as exc:  # noqa: BLE001
            log.error("on-connect history backfill conn=%s crashed: %s", conn_id, exc)
    threading.Thread(target=_run, name=f"hist-backfill-{conn_id}", daemon=True).start()


def heal_missing_history(*, limit: int = 50, start_year: int = HISTORY_START_YEAR) -> dict:
    """Scheduled SAFETY NET: backfill every connection whose history hasn't been
    pulled yet (history_backfilled_at IS NULL). Processes up to `limit` per run so
    a big batch can't blow the vendor rate caps in one tick; the rest heal next
    run. Idempotent — succeeds-once then never re-touches a connection.

    Returns {pending, processed, inserted, updated, stamped, still_pending}.
    """
    with SessionLocal() as db:
        pending_ids = list(db.execute(
            select(InverterConnection.id).where(
                InverterConnection.history_backfilled_at.is_(None),
            ).order_by(InverterConnection.id).limit(limit)
        ).scalars().all())
        total_pending = db.execute(
            select(InverterConnection.id).where(
                InverterConnection.history_backfilled_at.is_(None)
            )
        ).scalars().all()

    processed = ins = upd = stamped = 0
    for cid in pending_ids:
        try:
            r = backfill_connection_history(cid, start_year=start_year)
            processed += 1
            ins += r.get("inserted", 0)
            upd += r.get("updated", 0)
            stamped += 1 if r.get("stamped") else 0
        except Exception as exc:  # noqa: BLE001
            log.error("heal_missing_history conn=%s crashed: %s", cid, exc)
    result = {"pending": len(total_pending), "processed": processed,
              "inserted": ins, "updated": upd, "stamped": stamped,
              "still_pending": len(total_pending) - stamped}
    log.info("heal_missing_history: %s", result)
    return result
