"""Reconciling the money the household TALKS about with the money the data shows.

Three kinds of spending flow through this household, and each has a home:

* on a LIVE-FEED account (BofA, Fidelity) — the sync brings it in; recording a
  mention would double-count it.
* CASH / peer-to-peer — no source will ever show it; log_expense makes it a real
  row immediately.
* on a STATEMENT-FED account — the Apple Card: no feed exists, the data arrives
  only when someone emails a Wallet export, weeks later. A mention of such a
  spend is real knowledge with nowhere to live — so it lives HERE, as a pending
  expense, until the statement import confirms it.

The matcher is deliberately forgiving about what people say ("$840 for tires"
against a posted 838.60) and strict about never confirming twice: one pending
matches at most one transaction and vice versa, oldest mention first.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PendingExpense, Transaction

log = logging.getLogger("bankai.pending")

#: People round when they talk. $840 for a posted 838.60 is the same spend.
def amount_tolerance(amount: float) -> float:
    return max(1.0, abs(amount) * 0.02)


#: A charge can post a couple of days before someone mentions it…
MATCH_DAYS_BEFORE = 4
#: …and an Apple Card export can arrive well over a month after the spend.
MATCH_DAYS_AFTER = 60

#: Open this long with no statement showing it: worth saying out loud — the
#: export is overdue, or the charge never existed.
STALE_DAYS = 45


def _desc_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def _is_same_spend(a_amount: float, a_desc: str, b_amount: float, b_desc: str) -> bool:
    """Dedup ONLY a genuine re-mention: the SAME amount to the cent AND a shared
    word. The old guard used a $1-floor tolerance, which wrongly rejected two
    distinct small spends ($8 meds as a repeat of $7 hose). Logging every real
    expense matters far more than collapsing the rare true double-mention — a
    duplicate is cleaned up at reconciliation; a rejected spend is just lost."""
    if round(abs(a_amount - b_amount), 2) > 0.005:
        return False
    return bool(_desc_tokens(a_desc) & _desc_tokens(b_desc))


KINDS = ("itemized", "estimate")
STATUSES = ("open", "matched", "dismissed")


def note(
    session: Session,
    *,
    amount: float,
    description: str,
    account_hint: str = "",
    speaker: str = "",
    mentioned_on: date | None = None,
    kind: str = "itemized",
) -> tuple[PendingExpense, bool]:
    """Record a mentioned spend as pending. Returns (row, created).

    Re-mentions collapse: the same amount (within tolerance) with an open
    pending inside a week is the household talking about the same spend twice,
    not two spends — the copilot re-hearing "yeah, the $840 tires" must not
    manufacture an $1,680 hole. Estimates are exempt from this collapse: a rough
    "$500 of house stuff" and a real $500 charge are different things, and an
    estimate is meant to be revised, not deduped away.
    """
    amount = -abs(amount)
    kind = kind if kind in KINDS else "itemized"
    mentioned_on = mentioned_on or date.today()
    if kind == "itemized":
        window_start = mentioned_on - timedelta(days=3)
        for existing in session.execute(
            select(PendingExpense).where(
                PendingExpense.status == "open",
                PendingExpense.kind == "itemized",
                PendingExpense.mentioned_on >= window_start,
                PendingExpense.mentioned_on <= mentioned_on + timedelta(days=3),
            )
        ).scalars():
            if _is_same_spend(existing.amount, existing.description, amount, description):
                return existing, False
    row = PendingExpense(
        mentioned_on=mentioned_on,
        amount=round(amount, 2),
        description=description.strip()[:240],
        account_hint=account_hint.strip(),
        speaker=speaker.strip(),
        kind=kind,
    )
    session.add(row)
    session.flush()
    return row, True


def revise(
    session: Session,
    pending_id: str,
    *,
    amount: float | None = None,
    description: str | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> PendingExpense | None:
    """Adjust a pending entry as understanding improves — the lever the copilot
    uses to reconcile. Shrink an estimate as itemized detail is attributed to it
    (amount=), close one out (status='dismissed'), relabel it, or reclassify a
    lump as an estimate. Returns the row, or None if not found."""
    row = session.get(PendingExpense, pending_id)
    if row is None:
        return None
    if amount is not None:
        row.amount = -abs(round(float(amount), 2))
    if description is not None and description.strip():
        row.description = description.strip()[:240]
    if kind in KINDS:
        row.kind = kind
    if status in STATUSES:
        row.status = status
        if status != "open":
            row.resolved_at = datetime.utcnow()
    session.flush()
    return row


def summary(session: Session, today: date | None = None) -> dict:
    """A coherent picture that never double-counts: itemized spends plus the
    still-unexplained remainder of any estimates.

    Because the copilot shrinks an estimate as itemized detail is attributed to
    it, the raw sum of open amounts IS the honest total — this just splits it so
    the household sees 'known items' vs 'rough envelopes still to be pinned
    down'."""
    items = open_items(session, today)
    itemized = [i for i in items if i.get("kind") != "estimate"]
    estimates = [i for i in items if i.get("kind") == "estimate"]
    itemized_total = round(sum(i["amount"] for i in itemized), 2)
    estimate_total = round(sum(i["amount"] for i in estimates), 2)
    return {
        "itemized": itemized,
        "estimates": estimates,
        "itemized_total": itemized_total,
        "estimate_total": estimate_total,
        "net_total": round(itemized_total + estimate_total, 2),
    }


def open_items(session: Session, today: date | None = None) -> list[dict]:
    """Open pendings, oldest first, each honest about its age."""
    today = today or date.today()
    out = []
    for row in session.execute(
        select(PendingExpense)
        .where(PendingExpense.status == "open")
        .order_by(PendingExpense.mentioned_on)
    ).scalars():
        age = (today - row.mentioned_on).days
        out.append({
            "id": row.id,
            "mentioned_on": row.mentioned_on.isoformat(),
            "amount": row.amount,
            "description": row.description,
            "account_hint": row.account_hint,
            "speaker": row.speaker,
            "kind": row.kind,
            "age_days": age,
            "stale": age >= STALE_DAYS,
        })
    return out


def _hint_fits(hint: str, account_name: str) -> bool:
    if not hint:
        return True
    hint, name = hint.lower(), (account_name or "").lower()
    return hint in name or name in hint


def reconcile(session: Session, transaction_ids: list[str]) -> list[dict]:
    """Match newly imported transactions against open pending mentions.

    Called from the statement-import path with the ids that were ACTUALLY added
    (already deduped), so a re-import can never re-confirm anything. Greedy,
    oldest mention first; each side used at most once; best candidate = closest
    amount, then nearest date.
    """
    if not transaction_ids:
        return []
    txns = [t for t in (
        session.get(Transaction, tid) for tid in transaction_ids
    ) if t is not None and t.amount < 0]
    if not txns:
        return []
    # Only itemized mentions map to a single real charge. An estimate is an
    # envelope over many charges — it is reconciled by the copilot (shrunk or
    # dismissed), never auto-matched to one transaction.
    open_rows = list(session.execute(
        select(PendingExpense)
        .where(PendingExpense.status == "open", PendingExpense.kind == "itemized")
        .order_by(PendingExpense.mentioned_on)
    ).scalars())

    matches: list[dict] = []
    used: set[str] = set()
    for row in open_rows:
        tol = amount_tolerance(row.amount)
        earliest = row.mentioned_on - timedelta(days=MATCH_DAYS_BEFORE)
        latest = row.mentioned_on + timedelta(days=MATCH_DAYS_AFTER)
        candidates = [
            t for t in txns
            if t.id not in used
            and abs(t.amount - row.amount) <= tol
            and earliest <= t.posted <= latest
            and _hint_fits(row.account_hint, t.account.name if t.account else "")
        ]
        if not candidates:
            continue
        best = min(
            candidates,
            key=lambda t: (
                abs(t.amount - row.amount),
                abs((t.posted - row.mentioned_on).days),
            ),
        )
        used.add(best.id)
        row.status = "matched"
        row.matched_transaction_id = best.id
        row.resolved_at = datetime.utcnow()
        matches.append({
            "pending_id": row.id,
            "mentioned_on": row.mentioned_on.isoformat(),
            "said": f"{row.description} (~${abs(row.amount):.2f})",
            "posted": best.posted.isoformat(),
            "actual": f"{best.description} (${abs(best.amount):.2f})",
        })
        log.info("pending %s matched to txn %s", row.id, best.id)
    session.flush()
    return matches
