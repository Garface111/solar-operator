"""Data sponge — absorb an owner's FULL utility energy history at onboarding.

THE PRODUCT: the moment an owner connects their GMP login, we replay their
captured session server-side and suck in EVERYTHING GMP exposes for every
billing period they have (typically 3+ years) — generation, consumption,
sent-to-grid, cost, rate, net-metering credits — and store it as their energy
record. It's just THERE in their account, organized, the instant they connect.
That's the moat: years of their own data they can't easily get elsewhere, so
the switching cost compounds with every period absorbed.

This module orchestrates the absorb with live progress (SpongeProgress) so the
frontend can show a real "importing your 3 years…" progress bar, and reuses the
proven worker pull path (pull_bills_for_account → _pull_via_json) so the actual
GMP fetch/parse/persist logic is the same battle-tested code.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, func

from .db import SessionLocal
from .models import (
    Tenant, UtilityAccount, UtilitySession, Bill, SpongeProgress, now,
)
from . import sessions as _sessions

log = logging.getLogger("sponge")


def _progress(db, tenant_id: str, provider: str) -> SpongeProgress:
    row = db.execute(
        select(SpongeProgress).where(
            SpongeProgress.tenant_id == tenant_id,
            SpongeProgress.provider == provider,
        )
    ).scalar_one_or_none()
    if row is None:
        row = SpongeProgress(tenant_id=tenant_id, provider=provider)
        db.add(row)
        db.flush()
    return row


def absorb_history(tenant_id: str, provider: str = "gmp") -> dict:
    """Absorb the full bill history for every enabled account of this provider,
    updating SpongeProgress as it goes. Idempotent: re-running re-absorbs (the
    bill upsert dedupes by document/period). Returns a final summary.

    Designed to run as a background job fired right after a GMP capture lands.
    """
    from . import worker

    # Reset/seed progress to "running".
    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        if not tenant:
            return {"error": f"unknown tenant {tenant_id}"}
        accounts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant_id,
                UtilityAccount.provider == provider,
                UtilityAccount.enabled == True,  # noqa: E712
            )
        ).scalars().all()
        prog = _progress(db, tenant_id, provider)
        prog.status = "running"
        prog.accounts_total = len(accounts)
        prog.accounts_done = 0
        prog.bills_absorbed = 0
        prog.years_covered = None
        prog.message = f"Importing your {provider.upper()} history…"
        prog.error = None
        prog.started_at = now()
        prog.updated_at = now()
        account_ids = [a.id for a in accounts]
        db.commit()

    if not account_ids:
        with SessionLocal() as db:
            prog = _progress(db, tenant_id, provider)
            prog.status = "done"
            prog.message = "No accounts to import yet."
            prog.updated_at = now()
            db.commit()
        return {"status": "done", "accounts": 0}

    total_absorbed = 0
    errors: list[str] = []
    # Process one account at a time so the progress bar advances visibly.
    for i, acc_id in enumerate(account_ids, start=1):
        try:
            with SessionLocal() as db:
                account = db.get(UtilityAccount, acc_id)
                jwt = _sessions.token_for_account(db, account) if account else None
                if account is None or not jwt:
                    errors.append(f"account {acc_id}: no usable session")
                else:
                    from .adapters import get_adapter
                    adapter = get_adapter(account.provider)
                    res = worker._pull_via_json(db, tenant_id, account, adapter, jwt)
                    db.commit()
                    total_absorbed += int(res.get("created", 0)) + int(res.get("updated", 0))
        except Exception as exc:  # one bad account never aborts the whole absorb
            errors.append(f"account {acc_id}: {exc}")
            log.warning("sponge: account %s failed: %s", acc_id, exc, exc_info=True)

        # update progress after each account
        with SessionLocal() as db:
            prog = _progress(db, tenant_id, provider)
            prog.accounts_done = i
            prog.bills_absorbed = total_absorbed
            prog.years_covered = _years_covered(db, tenant_id)
            prog.message = f"Imported {total_absorbed} bills across {i}/{len(account_ids)} account(s)…"
            prog.updated_at = now()
            db.commit()

    with SessionLocal() as db:
        prog = _progress(db, tenant_id, provider)
        yrs = _years_covered(db, tenant_id)
        prog.status = "error" if (errors and total_absorbed == 0) else "done"
        prog.years_covered = yrs
        prog.bills_absorbed = total_absorbed
        if prog.status == "done":
            prog.message = (
                f"Imported {total_absorbed} bills — {yrs:.1f} years of your energy history."
                if yrs else f"Imported {total_absorbed} bills."
            )
        else:
            prog.message = "Couldn't import your history — please reconnect."
        prog.error = "; ".join(errors)[:1000] if errors else None
        prog.updated_at = now()
        db.commit()

    return {
        "status": "done" if not errors or total_absorbed else "error",
        "accounts": len(account_ids), "bills_absorbed": total_absorbed,
        "errors": errors,
    }


def _years_covered(db, tenant_id: str) -> float | None:
    """Span (in years) of absorbed bill history for this tenant — what the UI
    shows as 'N years of your energy history'."""
    lo, hi = db.execute(
        select(func.min(Bill.period_start), func.max(Bill.period_end))
        .where(Bill.tenant_id == tenant_id)
    ).one()
    if not lo or not hi:
        return None
    days = (hi - lo).days
    return round(days / 365.25, 1) if days > 0 else None


def rederive_from_raw(tenant_id: str | None = None, batch: int = 500) -> dict:
    """Re-parse the full energy record from already-stored raw_json — NO re-pull.

    When the GMP parser improves (e.g. learning the real cost/consumption field
    names), every bill we ever absorbed can be re-derived in place from its stored
    raw_json. This is the payoff of keeping raw_json: a parser fix retroactively
    enriches years of history instantly. Scope to one tenant or run fleet-wide.
    """
    from .adapters import gmp as _gmp
    updated = 0
    scanned = 0
    with SessionLocal() as db:
        q = select(Bill).where(Bill.raw_json.isnot(None))
        if tenant_id:
            q = q.where(Bill.tenant_id == tenant_id)
        rows = db.execute(q).scalars().all()
        for b in rows:
            scanned += 1
            full = _gmp._extract_full_record(b.raw_json or {})
            b.kwh_gross_generated = full["kwh_gross_generated"]
            cons = full["kwh_consumed_full"]
            b.kwh_consumed = int(round(cons)) if cons is not None else b.kwh_consumed
            b.kwh_sent_to_grid = full["kwh_sent_to_grid"]
            b.is_net_metered = full["is_net_metered"]
            b.total_cost = full["total_cost"]
            b.net_credit = full["net_credit"]
            b.solar_credit_usd = full["solar_credit_usd"]
            b.avg_rate_cents_kwh = full["avg_rate_cents_kwh"]
            b.supplier = full["supplier"]
            updated += 1
            if updated % batch == 0:
                db.commit()
        db.commit()
    return {"scanned": scanned, "updated": updated}


def sponge_status(tenant_id: str, provider: str = "gmp") -> dict:
    """Snapshot for the progress-bar poller. Always returns a usable shape."""
    with SessionLocal() as db:
        row = db.execute(
            select(SpongeProgress).where(
                SpongeProgress.tenant_id == tenant_id,
                SpongeProgress.provider == provider,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"status": "idle", "accounts_total": 0, "accounts_done": 0,
                    "bills_absorbed": 0, "years_covered": None, "pct": 0, "message": None}
        pct = int(round(100 * row.accounts_done / row.accounts_total)) if row.accounts_total else (
            100 if row.status == "done" else 0)
        return {
            "status": row.status,
            "accounts_total": row.accounts_total,
            "accounts_done": row.accounts_done,
            "bills_absorbed": row.bills_absorbed,
            "years_covered": row.years_covered,
            "pct": pct,
            "message": row.message,
            "error": row.error,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
