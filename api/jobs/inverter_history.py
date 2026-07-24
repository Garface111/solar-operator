"""Self-healing deep-history backfill for inverter connections.

THE PROBLEM IT SOLVES
  The nightly inverter pull (jobs/inverter_pull) only reaches ~90 days back. A
  freshly-connected SolarEdge/Fronius/SMA/Locus array therefore shows just the
  CURRENT year in Trends — its real multi-year history sits on the vendor's
  servers, never pulled.

REBUILT 2026-07-24 (REBUILD-MAP layer 3 — "no silent work"). The original
version had three flaws that produced the conn-95 incident (8 days "pending"
while its data quietly sat in the DB):
  • ONE giant end-of-run transaction — a web redeploy mid-run lost everything
    and nothing was visible until the very end. Now every year-chunk commits
    as it lands: data becomes usable immediately and a killed process loses at
    most one year.
  • STAMP-ONLY-ON-PERFECT-PASS — one failed year-chunk withheld the stamp
    forever, leaving a connection eternally "pending" with its data already
    present. Now a completed pass ALWAYS stamps; failed years are recorded on
    conn.last_error (tagged "history backfill:") and the healer retries those
    until a clean pass clears the tag.
  • ERRORS TO LOGS ONLY — nothing on the row, nothing in the UI, no alert.
    Now failures persist to last_error, every run emits job.updated events,
    each landed chunk emits generation.updated, and the healer alerts (24h
    dedup) when connections sit unstamped for more than a day.

  Vendor-agnostic via inverters.fetch_daily; chunks year-by-year (SolarEdge
  caps DAY energy at ~1 year/request). Upserts into DailyGeneration keyed
  (array_id, day); NEVER clobbers a non-vendor real source (csv/manual/
  utility_meter/gmp_api/...), only fills gaps + refreshes its own rows.
  Idempotent.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import events, inverters
from ..db import SessionLocal
from ..inverters import InverterError
from ..models import Array, DailyGeneration, InverterConnection, KVFlag, now

log = logging.getLogger(__name__)

HISTORY_START_YEAR = 2010
# Real metered sources from OTHER feeds we must never overwrite with a vendor
# history pull. (The vendor's own source string is allowed to refresh itself.)
_PROTECT_SOURCES = {
    "csv", "manual", "utility_meter", "gmp_api", "gmp_portal_scrape", "smarthub",
}
# last_error tag for a partial pass — the healer greps for this to retry, and a
# clean pass clears it. Keep the prefix stable.
_ERR_TAG = "history backfill:"
_ALERT_FLAG = "history_backfill:pending_alert"


def backfill_connection_history(
    conn_id: int, *, start_year: int = HISTORY_START_YEAR,
    db: Session | None = None,
) -> dict:
    """Pull the FULL multi-year daily history for ONE inverter connection.

    Chunk-commits per year (progress survives crashes; data is usable as it
    lands), stamps history_backfilled_at whenever the pass COMPLETES (even
    with failed years — those go to last_error for the healer), and emits
    job.updated / generation.updated events.

    Returns {connection_id, array_id, vendor, inserted, updated, first_day,
             years_probed, failed_years, stamped, error?}.
    """
    own = db is None
    if own:
        db = SessionLocal()
    tenant_id: str | None = None
    try:
        conn = db.get(InverterConnection, conn_id)
        if conn is None:
            return {"connection_id": conn_id, "error": "connection not found"}
        arr = db.get(Array, conn.array_id)
        if arr is None or arr.deleted_at is not None:
            # Orphaned/soft-deleted array — nothing live to show history for.
            # STAMP it so the healer stops retrying a dead connection forever.
            conn.history_backfilled_at = now()
            db.commit()
            return {"connection_id": conn_id, "array_id": conn.array_id,
                    "stamped": True, "note": "array missing/deleted — skipped"}

        tenant_id = arr.tenant_id
        vendor = conn.vendor
        module = inverters.VENDORS.get(vendor)
        if module is None or not getattr(module, "SUPPORTS_DAILY", False):
            # Nothing pullable (e.g. chint). Mark done so the healer stops
            # retrying — there is no history API to wait for.
            conn.history_backfilled_at = now()
            db.commit()
            return {"connection_id": conn_id, "array_id": arr.id, "vendor": vendor,
                    "inserted": 0, "updated": 0, "stamped": True,
                    "note": "vendor has no daily history pull"}

        events.publish(tenant_id, "job.updated", {
            "kind": "history_backfill", "status": "running",
            "connection_id": conn_id, "array_id": arr.id,
        })

        config = dict(conn.config or {})
        today = date.today()
        # Preload existing rows once (update-in-place without per-day queries).
        # SessionLocal has expire_on_commit=False, so this identity map stays
        # cheap across the per-chunk commits below.
        existing = {
            r.day: r for r in db.execute(
                select(DailyGeneration).where(DailyGeneration.array_id == arr.id)
            ).scalars().all()
        }
        ins = upd = 0
        first_day = None
        years_probed = 0
        failed_years: list[int] = []
        last_exc: str | None = None
        for yr in range(start_year, today.year + 1):
            chunk_start = date(yr, 1, 1)
            chunk_end = date(yr, 12, 31) if yr < today.year else today
            years_probed += 1
            try:
                entries = inverters.fetch_daily(vendor, config, chunk_start, chunk_end)
            except InverterError as exc:
                failed_years.append(yr)
                last_exc = str(exc)
                log.warning("history backfill conn=%s vendor=%s year=%s error=%s",
                            conn_id, vendor, yr, exc)
                continue
            except Exception as exc:  # noqa: BLE001 — one bad year mustn't kill the rest
                failed_years.append(yr)
                last_exc = str(exc)
                log.error("history backfill conn=%s year=%s unhandled: %s",
                          conn_id, yr, exc)
                continue
            chunk_wrote = 0
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
                        chunk_wrote += 1
                else:
                    # Race-safe insert: connect-path / nightly pull / sibling
                    # capture can commit the same (array_id, day) after our
                    # preload. SAVEPOINT + IntegrityError → re-read and apply
                    # the update path (never clobber protected sources).
                    try:
                        with db.begin_nested():
                            ng = DailyGeneration(
                                tenant_id=arr.tenant_id, array_id=arr.id,
                                day=d, kwh=kwh, source=vendor,
                            )
                            db.add(ng)
                            db.flush()
                        existing[d] = ng
                        ins += 1
                        chunk_wrote += 1
                    except IntegrityError:
                        row = db.execute(
                            select(DailyGeneration).where(
                                DailyGeneration.array_id == arr.id,
                                DailyGeneration.day == d,
                            )
                        ).scalar_one_or_none()
                        if row is None:
                            raise
                        existing[d] = row
                        if row.source in _PROTECT_SOURCES:
                            continue
                        if abs((row.kwh or 0) - kwh) > 1e-6 or row.source != vendor:
                            row.kwh = kwh
                            row.source = vendor
                            row.uploaded_at = now()
                            upd += 1
                            chunk_wrote += 1
            # CHUNK-COMMIT: the year's data is durable + visible right now.
            db.commit()
            if chunk_wrote:
                events.publish(tenant_id, "generation.updated", {
                    "array_id": arr.id, "year": yr,
                })

        # The pass COMPLETED — stamp regardless of failed years. Partial
        # failures go to last_error (tagged) so the healer retries them; a
        # clean pass clears any stale backfill tag. Eternal-pending is dead.
        conn.history_backfilled_at = now()
        if failed_years:
            conn.last_error = (
                f"{_ERR_TAG} years {','.join(map(str, failed_years))} "
                f"failed: {last_exc}"
            )[:500]
        elif (conn.last_error or "").startswith(_ERR_TAG):
            conn.last_error = None
        db.commit()
        events.publish(tenant_id, "job.updated", {
            "kind": "history_backfill",
            "status": "partial" if failed_years else "done",
            "connection_id": conn_id, "array_id": arr.id,
            "failed_years": failed_years,
        })
        return {"connection_id": conn_id, "array_id": arr.id, "vendor": vendor,
                "inserted": ins, "updated": upd,
                "first_day": str(first_day) if first_day else None,
                "years_probed": years_probed, "failed_years": failed_years,
                "stamped": True}
    except Exception as exc:
        # The pass itself died (not a per-year failure): persist WHY on the
        # row before re-raising, so a dead backfill is visible in the product
        # instead of only in a log stream nobody reads.
        try:
            db.rollback()
            conn = db.get(InverterConnection, conn_id)
            if conn is not None:
                conn.last_error = f"{_ERR_TAG} crashed: {exc}"[:500]
                db.commit()
            if tenant_id:
                events.publish(tenant_id, "job.updated", {
                    "kind": "history_backfill", "status": "failed",
                    "connection_id": conn_id,
                })
        except Exception:  # noqa: BLE001 — error-path bookkeeping must not mask exc
            log.exception("backfill error bookkeeping failed conn=%s", conn_id)
        raise
    finally:
        if own:
            db.close()


def backfill_connection_history_async(conn_id: int) -> None:
    """Fire the backfill in a daemon thread (the connect endpoint uses this so
    the HTTP response isn't blocked by a multi-year pull).

    A redeploy can still kill the thread — that is now SURVIVABLE: chunks
    already committed stay, last_error records a crash when catchable, and the
    hourly healer retries anything unstamped or error-tagged.
    """
    def _run():
        try:
            r = backfill_connection_history(conn_id)
            log.info("on-connect history backfill conn=%s: %s", conn_id, r)
        except Exception as exc:  # noqa: BLE001
            log.error("on-connect history backfill conn=%s crashed: %s", conn_id, exc)
    threading.Thread(target=_run, name=f"hist-backfill-{conn_id}", daemon=True).start()


def heal_missing_history(*, limit: int = 50, start_year: int = HISTORY_START_YEAR) -> dict:
    """Hourly SAFETY NET: retry every connection that is unstamped OR carries a
    tagged partial-failure last_error (stamped-with-errors older than 6h, so a
    persistently failing vendor is retried a few times a day, not hammered).

    Also ALERTS (once per 24h) when connections have sat unstamped for >24h —
    the conn-95 failure mode. Capped per-run to respect vendor rate limits.
    """
    from ..notify import send_internal_alert  # noqa: PLC0415 — avoid import cycle

    retry_before = now() - timedelta(hours=6)
    with SessionLocal() as db:
        unstamped = list(db.execute(
            select(InverterConnection.id).where(
                InverterConnection.history_backfilled_at.is_(None),
            ).order_by(InverterConnection.id)
        ).scalars().all())
        errored = list(db.execute(
            select(InverterConnection.id).where(
                InverterConnection.history_backfilled_at.is_not(None),
                InverterConnection.history_backfilled_at < retry_before,
                InverterConnection.last_error.like(f"{_ERR_TAG}%"),
            ).order_by(InverterConnection.id)
        ).scalars().all())
        # Stale-pending alert (dedup 24h via KVFlag).
        stale_cutoff = now() - timedelta(hours=24)
        stale = list(db.execute(
            select(InverterConnection.id).where(
                InverterConnection.history_backfilled_at.is_(None),
                InverterConnection.created_at < stale_cutoff,
            )
        ).scalars().all())
        if stale:
            flag = db.get(KVFlag, _ALERT_FLAG)
            last = flag.updated_at if flag else None
            if last is None or last < stale_cutoff:
                if flag is None:
                    db.add(KVFlag(key=_ALERT_FLAG, value=str(len(stale))))
                else:
                    flag.value = str(len(stale))
                    flag.updated_at = now()
                db.commit()
                send_internal_alert(
                    "Inverter history backfill: connections stuck >24h",
                    f"{len(stale)} connection(s) have had no successful history "
                    f"backfill for over 24h: ids {stale[:20]}. The hourly healer "
                    "is retrying; if this repeats, a vendor API or credential is "
                    "broken.",
                )

    pending_ids = (unstamped + errored)[:limit]
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
    result = {"unstamped": len(unstamped), "errored_retry": len(errored),
              "processed": processed, "inserted": ins, "updated": upd,
              "stamped": stamped}
    log.info("heal_missing_history: %s", result)
    return result
