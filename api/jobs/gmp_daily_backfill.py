"""
GMP daily-interval backfill — the multi-year DATA SPONGE for Green Mountain Power.

WHAT IT DOES
For each enabled GMP UtilityAccount, walk BACKWARD in <=90-day windows from today
until GMP says "no data here" (HTTP 404 = the meter's history floor), pulling the
15-minute interval generation CSV for each window. Every raw payload is stored
verbatim in GmpUsageRaw (THE SPONGE — attached to invoices later, re-derivable),
and per-day kWh is derived into GmpDailyGeneration (the queryable layer the Reports
read-contract serves).

WHY WINDOWED + BACKWARD (grounded on a live read-only probe, 2026-06-18):
  • A ~1-year request 503-TIMES-OUT server-side on every account. 60-day windows
    return reliably (≈5,760 rows). So we page at WINDOW_DAYS (default 60).
  • Daily history depth is PER-METER and is NOT the bills endpoint's 16 years.
    Below a meter's earliest data GMP returns a clean 404. Walking backward until
    the 404 lets each meter self-report its true floor — no assumed start year.

SAFETY / CORRECTNESS
  • Idempotent: GmpUsageRaw upserts by (account_id, window_start, window_end);
    GmpDailyGeneration upserts by (account_id, day). Re-running re-pulls only the
    recent (still-changing) windows and any gaps; settled past windows are skipped
    when already stored unless force_refetch=True.
  • Token refresh: a 401/403 triggers one gmp_refresh.refresh_gmp_token retry; the
    refreshed token is persisted back to the session row so the next account reuses it.
  • Per-account isolation: one account's failure never aborts the run.
  • NO daylight gate: this is settled historical data, not live power. (The
    daylight gate applies only to the live poller, not backfill.)
  • NEVER fabricates: a window that 404s or errors writes NOTHING for that range;
    derived kWh is only ever the summed real interval Quantity from a real payload.

ENTRY POINTS
  backfill_account(db, account_id, ...) -> dict     # one account, full history
  backfill_tenant(tenant_id, ...) -> dict           # every enabled GMP account
  rederive_account(db, account_id) -> dict          # re-derive daily from stored raw, NO re-pull
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import (
    UtilityAccount, UtilitySession, GmpUsageRaw, GmpDailyGeneration, now,
)
from ..adapters import gmp as gmp_adapter
from ..adapters.gmp import GmpUsageNotFound, GmpUsageTimeout
from .. import sessions as _sessions
from .. import gmp_refresh

log = logging.getLogger("solar_operator.jobs.gmp_daily_backfill")

WINDOW_DAYS = 60          # <=90 (GMP 503s above ~90); 60 gives headroom
MAX_WINDOWS = 400         # hard stop (~65 years of 60d windows) — safety, never hit
EMPTY_STREAK_STOP = 2     # consecutive 404/empty windows that mean "below floor"
MIN_FLOOR_DATE = date(2000, 1, 1)  # absolute sanity floor; never page below this
# A non-200/non-404 window used to stop the walk immediately and mark the
# account "ok" anyway -- a transient GMP hiccup silently under-filled history
# forever. Retry with backoff before giving up (Ford, 2026-07-08: "find every
# instance of us intentionally sabotaging our own reliability").
WINDOW_ERROR_RETRIES = 3
WINDOW_ERROR_BACKOFF_SECONDS = (2, 5, 15)


def _usable_session(db: Session, account: UtilityAccount) -> Optional[UtilitySession]:
    """The session row whose login can read this account (per-customer, else latest)."""
    return _sessions.session_for_account(db, account)


def _persist_window(
    db: Session,
    account: UtilityAccount,
    ws: date,
    we: date,
    csv_text: str | None,
    parsed: dict,
    http_status: int = 200,
) -> tuple[int, int]:
    """Upsert ONE window's raw payload (sponge) + derived per-day rows.
    Returns (daily_inserted, daily_updated)."""
    # ── 1. raw sponge (verbatim) — idempotent on (account, window) ──
    raw = db.execute(
        select(GmpUsageRaw).where(
            GmpUsageRaw.account_id == account.id,
            GmpUsageRaw.window_start == ws,
            GmpUsageRaw.window_end == we,
        )
    ).scalar_one_or_none()
    if raw is None:
        raw = GmpUsageRaw(
            tenant_id=account.tenant_id, account_id=account.id,
            account_number=account.account_number, window_start=ws, window_end=we,
        )
        db.add(raw)
    raw.fmt = "csv"
    raw.http_status = http_status
    raw.raw_csv = csv_text
    raw.row_count = parsed["row_count"]
    raw.interval_min = parsed["interval_min"]
    raw.interval_max = parsed["interval_max"]
    raw.fetched_at = now()

    # ── 2. derived per-day rows — idempotent on (account, day) ──
    inserted = updated = 0
    by_day = parsed["by_day"]
    if by_day:
        days = list(by_day.keys())
        existing = {
            r.day: r for r in db.execute(
                select(GmpDailyGeneration).where(
                    GmpDailyGeneration.account_id == account.id,
                    GmpDailyGeneration.day.in_(days),
                )
            ).scalars().all()
        }
        for d, agg in by_day.items():
            kwh = agg["kwh"]
            # Settled solar generation should be >= 0; a tiny negative is meter
            # noise. Clamp negatives to 0 in the modeled layer (raw keeps truth).
            if kwh < 0:
                kwh = 0.0
            row = existing.get(d)
            if row is None:
                db.add(GmpDailyGeneration(
                    tenant_id=account.tenant_id, account_id=account.id,
                    account_number=account.account_number, array_id=account.array_id,
                    day=d, kwh=kwh, interval_count=agg["intervals"], source="gmp_api",
                ))
                inserted += 1
            else:
                row.kwh = kwh
                row.interval_count = agg["intervals"]
                row.array_id = account.array_id
                row.source = "gmp_api"
                row.derived_at = now()
                updated += 1
    return inserted, updated


def _refresh_token(db: Session, sess: UtilitySession) -> Optional[str]:
    """Refresh + persist a session's JWT. Returns the new token or None."""
    if not sess.refresh_token:
        return None
    try:
        new_jwt, exp = gmp_refresh.refresh_gmp_token(sess.refresh_token)
    except gmp_refresh.GmpRefreshError as exc:
        log.warning("gmp backfill: token refresh failed for session %s: %s", sess.id, exc)
        sess.refresh_failures = (sess.refresh_failures or 0) + 1
        return None
    sess.api_token = new_jwt
    sess.expires_at = exp
    sess.last_refresh_at = now()
    sess.refresh_failures = 0
    db.flush()
    return new_jwt


def backfill_account(
    db: Optional[Session],
    account_id: int,
    *,
    window_days: int = WINDOW_DAYS,
    force_refetch: bool = False,
    max_windows: int = MAX_WINDOWS,
) -> dict[str, Any]:
    """Backfill the FULL available daily history for one GMP account.

    force_refetch=False (default): skip windows already stored in the sponge
    (idempotent incremental); always re-pulls the most-recent window (still
    changing). True: re-pull every window (e.g. after a parser fix — though
    rederive_account is cheaper for that).

    Returns an evidence summary: windows fetched, rows, the REAL date range GMP
    returned, and the discovered history floor.
    """
    _own = db is None
    if _own:
        db = SessionLocal()
    summary: dict[str, Any] = {
        "account_id": account_id, "status": "ok",
        "windows_fetched": 0, "windows_skipped": 0, "windows_404": 0,
        "raw_rows_total": 0, "daily_inserted": 0, "daily_updated": 0,
        "earliest_day": None, "latest_day": None, "history_floor": None,
        "errors": [],
    }
    try:
        account = db.get(UtilityAccount, account_id)
        if account is None or account.provider != "gmp":
            summary["status"] = "skipped"
            summary["errors"].append("not a GMP account")
            return summary
        if not account.account_number:
            summary["status"] = "skipped"
            summary["errors"].append("no account_number")
            return summary

        sess = _usable_session(db, account)
        if sess is None or not sess.api_token:
            summary["status"] = "skipped"
            summary["errors"].append("no usable session token — owner must reconnect GMP")
            return summary
        jwt = sess.api_token

        # pre-emptive refresh if expired/near-expiry
        if sess.expires_at and sess.expires_at <= now():
            new = _refresh_token(db, sess)
            if new:
                jwt = new

        # which windows are already stored (skip set), keyed by (ws, we)
        stored: set[tuple] = set()
        if not force_refetch:
            for r in db.execute(
                select(GmpUsageRaw.window_start, GmpUsageRaw.window_end,
                       GmpUsageRaw.http_status).where(
                    GmpUsageRaw.account_id == account.id)
            ).all():
                # keep 404 windows in the skip set too (don't re-probe the floor)
                stored.add((r[0], r[1]))

        end = date.today() + timedelta(days=1)  # inclusive of today
        empty_streak = 0
        earliest = latest = None
        floor: Optional[date] = None

        for _ in range(max_windows):
            start = end - timedelta(days=window_days)
            if start < MIN_FLOOR_DATE:
                break
            key = (start, end)
            # Always re-pull the newest window (idx 0, still-changing data);
            # skip already-stored older windows unless force_refetch.
            is_newest = summary["windows_fetched"] == 0 and summary["windows_skipped"] == 0
            if not force_refetch and key in stored and not is_newest:
                summary["windows_skipped"] += 1
                end = start
                continue

            csv_text, parsed, status_code, retried = _fetch_one_window(
                db, sess, account, jwt, start, end
            )
            if retried and sess.api_token != jwt:
                jwt = sess.api_token  # refreshed mid-loop

            if status_code == 404:
                # Below this meter's floor. Record a 404 sponge marker so we
                # don't re-probe, and count toward the empty streak.
                _record_404(db, account, start, end)
                summary["windows_404"] += 1
                empty_streak += 1
                if empty_streak >= EMPTY_STREAK_STOP:
                    floor = floor or (latest and start)
                    break
                end = start
                continue
            if status_code != 200:
                # Retry with backoff before giving up on this window -- a single
                # GMP hiccup used to permanently stop the walk here AND still
                # report the account "ok", silently under-filling history with
                # no way to tell (Ford, 2026-07-08: "find every instance of us
                # intentionally sabotaging our own reliability").
                for attempt, backoff in enumerate(WINDOW_ERROR_BACKOFF_SECONDS, start=1):
                    time.sleep(backoff)
                    csv_text, parsed, status_code, retried = _fetch_one_window(
                        db, sess, account, jwt, start, end
                    )
                    if retried and sess.api_token != jwt:
                        jwt = sess.api_token
                    if status_code == 200:
                        break
                    log.warning("gmp backfill: account %s window %s..%s retry %d/%d got HTTP %s",
                               account_id, start, end, attempt, WINDOW_ERROR_RETRIES, status_code)
                if status_code != 200:
                    summary["errors"].append(f"{start}..{end}: HTTP {status_code} (gave up after "
                                             f"{WINDOW_ERROR_RETRIES} retries)")
                    # Genuinely stuck -- stop walking deeper, but be HONEST that this
                    # account's history is incomplete rather than silently "ok".
                    summary["status"] = "partial"
                    break

            empty_streak = 0
            ins, upd = _persist_window(db, account, start, end, csv_text, parsed, 200)
            summary["windows_fetched"] += 1
            summary["raw_rows_total"] += parsed["row_count"]
            summary["daily_inserted"] += ins
            summary["daily_updated"] += upd
            if parsed["interval_min"]:
                earliest = parsed["interval_min"] if earliest is None else min(earliest, parsed["interval_min"])
            if parsed["interval_max"]:
                latest = parsed["interval_max"] if latest is None else max(latest, parsed["interval_max"])
            # commit per window so a later failure can't lose absorbed data
            db.commit()
            end = start

        floor = floor or earliest
        summary["earliest_day"] = earliest.isoformat() if earliest else None
        summary["latest_day"] = latest.isoformat() if latest else None
        summary["history_floor"] = floor.isoformat() if floor else None
        db.commit()
        return summary
    except Exception as exc:  # never let one account abort a fleet run
        summary["status"] = "error"
        summary["errors"].append(str(exc))
        log.warning("gmp backfill: account %s failed: %s", account_id, exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return summary
    finally:
        if _own:
            db.close()


def _fetch_one_window(db, sess, account, jwt, start, end):
    """Fetch + parse one window, with a single token-refresh retry on auth
    failure and a window-shrink retry on a 503 timeout.
    Returns (csv_text, parsed_dict, status_code, token_was_refreshed)."""
    retried = False
    try:
        csv_text = gmp_adapter.fetch_usage_csv(account.account_number, jwt, start, end)
    except GmpUsageNotFound:
        return "", {"by_day": {}, "row_count": 0, "interval_min": None,
                    "interval_max": None, "service_agreements": [], "unit": None}, 404, retried
    except GmpUsageTimeout:
        # window too big for the server right now — shrink to half and try once
        mid = start + timedelta(days=max(1, (end - start).days // 2))
        try:
            csv_text = gmp_adapter.fetch_usage_csv(account.account_number, jwt, mid, end)
            parsed = gmp_adapter.parse_usage_csv_to_daily(csv_text)
            return csv_text, parsed, 200, retried
        except Exception:
            return "", _empty_parsed(), 503, retried
    except ValueError as exc:
        msg = str(exc)
        if ("401" in msg or "403" in msg) and sess.refresh_token:
            new = _refresh_token(db, sess)
            if new:
                retried = True
                try:
                    csv_text = gmp_adapter.fetch_usage_csv(account.account_number, new, start, end)
                except GmpUsageNotFound:
                    return "", _empty_parsed(), 404, retried
                except Exception as exc2:
                    return "", _empty_parsed(), _status_of(exc2), retried
                parsed = gmp_adapter.parse_usage_csv_to_daily(csv_text)
                return csv_text, parsed, 200, retried
        return "", _empty_parsed(), _status_of(exc), retried
    except Exception as exc:
        return "", _empty_parsed(), _status_of(exc), retried
    parsed = gmp_adapter.parse_usage_csv_to_daily(csv_text)
    return csv_text, parsed, 200, retried


def _empty_parsed() -> dict:
    return {"by_day": {}, "row_count": 0, "interval_min": None,
            "interval_max": None, "service_agreements": [], "unit": None}


def _status_of(exc: Exception) -> int:
    s = str(exc)
    for code in ("404", "401", "403", "500", "502", "503"):
        if code in s:
            return int(code)
    return 599  # unknown


def _record_404(db: Session, account: UtilityAccount, ws: date, we: date) -> None:
    """Store a 404 marker so the backfill won't re-probe below the floor."""
    raw = db.execute(
        select(GmpUsageRaw).where(
            GmpUsageRaw.account_id == account.id,
            GmpUsageRaw.window_start == ws, GmpUsageRaw.window_end == we,
        )
    ).scalar_one_or_none()
    if raw is None:
        raw = GmpUsageRaw(
            tenant_id=account.tenant_id, account_id=account.id,
            account_number=account.account_number, window_start=ws, window_end=we,
        )
        db.add(raw)
    raw.fmt = "csv"
    raw.http_status = 404
    raw.raw_csv = None
    raw.row_count = 0
    raw.fetched_at = now()
    db.commit()


def backfill_tenant(
    tenant_id: str, *, window_days: int = WINDOW_DAYS, force_refetch: bool = False,
) -> dict[str, Any]:
    """Backfill every enabled GMP account for a tenant. Per-account isolation."""
    with SessionLocal() as db:
        accts = db.execute(
            select(UtilityAccount.id).where(
                UtilityAccount.tenant_id == tenant_id,
                UtilityAccount.provider == "gmp",
                UtilityAccount.enabled == True,  # noqa: E712
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all()
    results = []
    totals = {"daily_inserted": 0, "daily_updated": 0, "raw_rows_total": 0,
              "accounts_ok": 0, "accounts_partial": 0, "accounts_error": 0,
              "accounts_skipped": 0}
    for aid in accts:
        r = backfill_account(None, aid, window_days=window_days, force_refetch=force_refetch)
        results.append(r)
        totals["daily_inserted"] += r["daily_inserted"]
        totals["daily_updated"] += r["daily_updated"]
        totals["raw_rows_total"] += r["raw_rows_total"]
        if r["status"] == "ok":
            totals["accounts_ok"] += 1
        elif r["status"] == "partial":
            # Retried with backoff and still hit a wall -- got SOME real data,
            # unlike "skipped" (never even tried). Distinct bucket so a
            # persistently-incomplete account is visible, not folded into "ok".
            totals["accounts_partial"] += 1
        elif r["status"] == "error":
            totals["accounts_error"] += 1
        else:
            totals["accounts_skipped"] += 1
    return {"tenant_id": tenant_id, "accounts": len(accts), "totals": totals,
            "per_account": results}


def rederive_account(db: Optional[Session], account_id: int) -> dict[str, Any]:
    """Re-derive GmpDailyGeneration from already-stored GmpUsageRaw — NO re-pull.
    Use after a parser change to retroactively enrich stored history instantly."""
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        account = db.get(UtilityAccount, account_id)
        if account is None:
            return {"account_id": account_id, "status": "skipped", "reason": "no account"}
        raws = db.execute(
            select(GmpUsageRaw).where(
                GmpUsageRaw.account_id == account_id,
                GmpUsageRaw.raw_csv.isnot(None),
            )
        ).scalars().all()
        ins = upd = 0
        for raw in raws:
            parsed = gmp_adapter.parse_usage_csv_to_daily(raw.raw_csv or "")
            i, u = _persist_window(db, account, raw.window_start, raw.window_end,
                                   raw.raw_csv, parsed, raw.http_status)
            ins += i
            upd += u
        db.commit()
        return {"account_id": account_id, "status": "ok",
                "windows_rederived": len(raws), "daily_inserted": ins, "daily_updated": upd}
    finally:
        if _own:
            db.close()
