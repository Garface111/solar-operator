"""
Array classification: single_site vs group_net_metered.

Per spec §2. Operates on data already in the DB (no schema change). The primary
signal is the utility_accounts.extra->>'groupNetMetered' JSON flag; bill raw_text
markers are a confirming secondary signal recorded for observability only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import UtilityAccount, Bill


GROUP_RAW_MARKERS = ("group excess shared", "group rate")


@dataclass
class Classification:
    array_id: int
    classification: str  # "single_site" | "group_net_metered"
    raw_text_confirmed: bool
    host_account_id: int | None  # the authoritative generation account for group arrays
    needs_review: bool
    notes: list[str] = field(default_factory=list)


def _flag_true(extra: dict | None, key: str) -> bool:
    """JSON value may be a real bool or the string 'true' — accept either."""
    if not extra:
        return False
    v = extra.get(key)
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def raw_text_confirms_group(raw_text: str | None) -> bool:
    if not raw_text:
        return False
    t = raw_text.lower()
    return any(m in t for m in GROUP_RAW_MARKERS)


def _array_accounts(db: Session, array_id: int) -> list[UtilityAccount]:
    return list(
        db.execute(
            select(UtilityAccount).where(
                UtilityAccount.array_id == array_id,
                UtilityAccount.deleted_at.is_(None),
            )
        ).scalars()
    )


def _latest_parsed_bill(db: Session, account_ids: list[int]) -> Bill | None:
    if not account_ids:
        return None
    return db.execute(
        select(Bill)
        .where(Bill.account_id.in_(account_ids), Bill.parse_status == "parsed")
        .order_by(Bill.period_end.desc().nullslast())
        .limit(1)
    ).scalar_one_or_none()


def _select_host_account(
    db: Session, accounts: list[UtilityAccount], window_start, window_end
) -> int | None:
    """Host generation account = the group account with the greatest summed
    kwh_generated over the window. Falls back to all-time if window is empty."""
    group_accts = [a for a in accounts if _flag_true(a.extra, "groupNetMetered")]
    if not group_accts:
        group_accts = accounts  # defensive: treat all as candidates
    best_id, best_kwh = None, -1.0
    for a in group_accts:
        q = select(Bill).where(Bill.account_id == a.id, Bill.parse_status == "parsed")
        total = 0.0
        for b in db.execute(q).scalars():
            if b.kwh_generated is None:
                continue
            if window_start and window_end and b.period_end is not None:
                if not (b.period_end.date() >= window_start and (b.period_start.date() if b.period_start else b.period_end.date()) <= window_end):
                    continue
            total += float(b.kwh_generated)
        if total > best_kwh:
            best_id, best_kwh = a.id, total
    return best_id


def classify_array(db: Session, array_id: int, window_start=None, window_end=None) -> Classification:
    accounts = _array_accounts(db, array_id)
    flag_group = any(_flag_true(a.extra, "groupNetMetered") for a in accounts)
    latest = _latest_parsed_bill(db, [a.id for a in accounts])
    confirmed = raw_text_confirms_group(latest.raw_text if latest else None)

    # CALIBRATION (real-data finding, Bruce pilot): the groupNetMetered JSON flag
    # is set on ~63/64 arrays — it marks PARTICIPATION in group net metering, not
    # the role of HOST GENERATION METER. The authoritative signal that an account
    # is a group host meter (settling a full multi-inverter system that a single
    # production feed cannot match) is the bill raw_text marker "Group Excess
    # Shared" / "Group Rate". So raw_text is PRIMARY here; the JSON flag is a weak
    # corroborator only. A single-building account carrying the flag but lacking
    # the bill markers (e.g. Cover Catamount) is treated single_site.
    notes: list[str] = []
    needs_review = False
    classification = "group_net_metered" if confirmed else "single_site"

    if flag_group and not confirmed:
        # Extremely common in this data — NOT a problem; the flag is noisy.
        notes.append("groupNetMetered flag set but bill shows no host-meter markers "
                     "(participation flag, not a host meter) — treated single_site.")
    if confirmed and not flag_group:
        needs_review = True
        notes.append("Bill raw_text shows group host markers but groupNetMetered flag "
                     "missing — treated as group pending human confirm.")

    host_account_id = None
    if classification == "group_net_metered":
        host_account_id = _select_host_account(db, accounts, window_start, window_end)
    elif len(accounts) == 1:
        host_account_id = accounts[0].id

    return Classification(
        array_id=array_id,
        classification=classification,
        raw_text_confirmed=confirmed,
        host_account_id=host_account_id,
        needs_review=needs_review,
        notes=notes,
    )
