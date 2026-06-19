"""
SmartHub server-side daily generation pull.

Pulls daily generation data from the SmartHub JSON API for arrays whose
utility accounts have a stored authorizationToken (captured by the extension
during login). Upserts into the DailyGeneration table.

Requires that the extension has already:
  1. Captured the authorizationToken from the SmartHub login response
  2. Stored it in UtilitySession.api_token via /v1/sync

Entry point: pull_daily_generation_for_account()

If no valid session exists, the function skips silently — no error, no data.
Re-running the same date range is safe (upsert by array_id + day).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.smarthub import (
    fetch_account_list,
    fetch_daily_generation,
    is_smarthub_provider,
)
from ..db import SessionLocal
from ..models import Array, DailyGeneration, UtilityAccount, UtilitySession, now
from ..sessions import session_for_account

log = logging.getLogger("solar_operator.jobs.smarthub_pull")


def _latest_smarthub_session(
    db: Session, tenant_id: str, provider: str
) -> UtilitySession | None:
    return db.execute(
        select(UtilitySession)
        .where(
            UtilitySession.tenant_id == tenant_id,
            UtilitySession.provider == provider,
        )
        .order_by(UtilitySession.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _build_session_dict(sess: UtilitySession) -> dict[str, Any] | None:
    """Reconstruct a smarthub.py session dict from a stored UtilitySession row.

    Returns None if the session lacks a usable auth token.
    """
    token = sess.api_token
    if not token:
        return None
    user: dict = (sess.raw_payload or {}).get("user") or {}
    email = user.get("email") or user.get("username") or ""
    primary_username = user.get("primary_username") or user.get("username") or email
    if not email:
        return None
    return {
        "auth_token": token,
        "primary_username": primary_username,
        "email": email,
        # Treat stored sessions as valid (expiry managed by the capture flow)
        "expires_at": datetime.utcnow() + timedelta(minutes=5),
    }


def _get_smarthub_host(provider: str) -> str | None:
    from ..adapters.smarthub import PROVIDER_TO_UTILITY
    info = PROVIDER_TO_UTILITY.get(provider)
    return info["host"] if info else None


def pull_daily_generation_for_account(
    db: Session | None,
    tenant_id: str,
    array_id: int,
    days_back: int = 90,
) -> dict[str, Any]:
    """Pull daily generation from SmartHub for one array; upsert DailyGeneration.

    Args:
        db: optional open SQLAlchemy session. If None, opens its own.
        tenant_id: tenant owning the array.
        array_id: ID of the Array whose utility account to pull from.
        days_back: how many days back from today to pull.

    Returns a summary dict with keys: array_id, provider, inserted, updated, skipped, status.

    Idempotent: re-running the same range merges cleanly via upsert.
    Source is set to "smarthub" in DailyGeneration rows.
    """
    _own_db = db is None
    if _own_db:
        db = SessionLocal()

    try:
        arr = db.get(Array, array_id)
        if arr is None or arr.tenant_id != tenant_id:
            return {"array_id": array_id, "status": "skipped", "reason": "array not found"}

        # Find an enabled utility account for this array that is a SmartHub provider
        acct: UtilityAccount | None = None
        for a in db.execute(
            select(UtilityAccount).where(
                UtilityAccount.array_id == array_id,
                UtilityAccount.tenant_id == tenant_id,
                UtilityAccount.enabled.is_(True),
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars().all():
            if is_smarthub_provider(a.provider):
                acct = a
                break

        if acct is None:
            return {
                "array_id": array_id,
                "status": "skipped",
                "reason": "no enabled SmartHub account linked to this array",
            }

        provider = acct.provider
        host = _get_smarthub_host(provider)
        if not host:
            return {
                "array_id": array_id,
                "status": "skipped",
                "reason": f"unknown SmartHub provider {provider!r}",
            }

        # Bound to THIS account's login identity (customer_number), falling back
        # to the latest session for the provider — so an operator with separate
        # SmartHub logins per client keeps every login usable.
        sess_row = session_for_account(db, acct)
        if sess_row is None:
            return {
                "array_id": array_id,
                "status": "skipped",
                "reason": "no stored session — sign in via portal to enable server-side pull",
            }

        session_dict = _build_session_dict(sess_row)
        if session_dict is None:
            return {
                "array_id": array_id,
                "status": "skipped",
                "reason": "stored session has no auth token (pre-v1.5 capture)",
            }

        # Look up serviceLocationNumber from account extra, or discover it
        service_location: str | None = (acct.extra or {}).get("service_location_number")
        if not service_location:
            try:
                locations = fetch_account_list(host, session_dict)
                # Match by account_number where possible
                for loc in locations:
                    if loc["account_number"] == acct.account_number:
                        service_location = loc["service_location_number"]
                        break
                if not service_location and locations:
                    service_location = locations[0]["service_location_number"]
                # Cache in account extra for next run
                if service_location:
                    acct.extra = {**(acct.extra or {}), "service_location_number": service_location}
                    db.flush()
            except Exception as exc:
                log.warning(
                    "fetch_account_list failed for array %s provider %s: %s",
                    array_id, provider, exc,
                )
                return {
                    "array_id": array_id,
                    "status": "error",
                    "reason": f"fetch_account_list failed: {exc}",
                }

        if not service_location:
            return {
                "array_id": array_id,
                "status": "skipped",
                "reason": "could not discover serviceLocationNumber",
            }

        end_date = date.today()
        start_date = end_date - timedelta(days=days_back)

        try:
            rows = fetch_daily_generation(
                host=host,
                session=session_dict,
                service_location=service_location,
                account_number=acct.account_number,
                start=start_date,
                end=end_date,
            )
        except Exception as exc:
            log.warning(
                "fetch_daily_generation failed for array %s: %s", array_id, exc,
            )
            return {
                "array_id": array_id,
                "status": "error",
                "reason": f"fetch_daily_generation failed: {exc}",
            }

        if not rows:
            return {"array_id": array_id, "provider": provider, "status": "ok", "inserted": 0, "updated": 0}

        # Upsert by (array_id, day)
        days = [r["day"] for r in rows]
        existing = {
            row.day: row
            for row in db.execute(
                select(DailyGeneration).where(
                    DailyGeneration.array_id == array_id,
                    DailyGeneration.day.in_(days),
                )
            ).scalars().all()
        }

        inserted = 0
        updated = 0
        for row in rows:
            kwh = row["kwh_generated"]
            if kwh < 0:
                continue
            if row["day"] in existing:
                existing[row["day"]].kwh = kwh
                existing[row["day"]].source = "smarthub"
                existing[row["day"]].uploaded_at = now()
                updated += 1
            else:
                db.add(DailyGeneration(
                    tenant_id=tenant_id,
                    array_id=array_id,
                    day=row["day"],
                    kwh=kwh,
                    source="smarthub",
                ))
                inserted += 1

        if _own_db:
            db.commit()

        return {
            "array_id": array_id,
            "provider": provider,
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "inserted": inserted,
            "updated": updated,
            "total_rows": len(rows),
            "status": "ok",
        }

    finally:
        if _own_db:
            db.close()


def pull_all_smarthub(days_back: int = 90) -> dict[str, Any]:
    """Pull daily generation for EVERY array whose linked utility account is a
    SmartHub provider AND has a stored session with an auth token.

    Mirrors jobs.inverter_pull.pull_all_inverters: iterate candidates, pull each,
    aggregate counts. SmartHub (VEC/WEC/Stowe/…) bills carry NO generation kWh —
    a net-metering solar account's production lives only in the usage API, so
    this server-side pull is the authoritative source of DailyGeneration for
    those arrays. Accounts without a captured token are skipped silently (the
    extension must capture the authorizationToken first).

    Idempotent: per-account upsert by (array_id, day). Best-effort per account —
    one failure never aborts the batch.
    """
    summary = {"arrays_processed": 0, "arrays_with_data": 0,
               "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    with SessionLocal() as db:
        # Distinct arrays that have an enabled SmartHub utility account.
        rows = db.execute(
            select(UtilityAccount.array_id, UtilityAccount.tenant_id,
                   UtilityAccount.provider)
            .where(
                UtilityAccount.array_id.is_not(None),
                UtilityAccount.enabled.is_(True),
                UtilityAccount.deleted_at.is_(None),
            )
        ).all()
        candidates: list[tuple[int, str]] = []
        seen: set[int] = set()
        for array_id, tenant_id, provider in rows:
            if array_id in seen:
                continue
            if is_smarthub_provider(provider or ""):
                seen.add(array_id)
                candidates.append((array_id, tenant_id))

    for array_id, tenant_id in candidates:
        summary["arrays_processed"] += 1
        try:
            r = pull_daily_generation_for_account(None, tenant_id, array_id,
                                                  days_back=days_back)
            status = r.get("status")
            if status == "ok":
                ins = r.get("inserted", 0)
                upd = r.get("updated", 0)
                summary["inserted"] += ins
                summary["updated"] += upd
                if ins or upd:
                    summary["arrays_with_data"] += 1
            elif status == "skipped":
                summary["skipped"] += 1
            else:
                summary["errors"] += 1
        except Exception:
            summary["errors"] += 1
            log.exception("pull_all_smarthub: array %s failed", array_id)
    return summary
