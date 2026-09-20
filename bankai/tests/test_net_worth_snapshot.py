"""The daily net-worth snapshot must agree with get_accounts to the cent.

Snapshots are otherwise written one account at a time, only when that account moves,
so a day's history row used to sum only the accounts that happened to change that day
— and manually tracked accounts (a property, a mortgage, the Apple Card), which move
rarely, silently dropped out of the total. These tests pin the two halves of the fix:
snapshot_net_worth() stamps the whole set atomically, and net_worth_history() carries
each account forward so every day reflects the same set net_worth() sums.
"""
from datetime import date, timedelta

from sqlalchemy import select

from bankai.intelligence.insights import (
    net_worth,
    net_worth_history,
    snapshot_net_worth,
)
from bankai.models import Account, BalanceSnapshot

TODAY = date.today()


def _account(session, name, balance, *, kind="checking", source="simplefin"):
    account = Account(name=name, kind=kind, source=source, balance=balance)
    session.add(account)
    session.flush()
    return account


def _snap(session, account, days_ago, balance):
    row = BalanceSnapshot(
        account_id=account.id, date=TODAY - timedelta(days=days_ago), balance=balance
    )
    session.add(row)
    session.flush()
    return row


# --- test plan #1: snapshot(today) == sum(get_accounts) to the cent ---

def test_snapshot_total_equals_net_worth_to_the_cent(session):
    _account(session, "Adv Plus Banking", 20_108.81)
    _account(session, "Home", 1_398_000.00, kind="property", source="manual")
    _account(session, "Mortgage", -1_226_301.14, kind="mortgage", source="manual")
    _account(session, "Apple Card", -842.19, kind="credit", source="manual")

    result = snapshot_net_worth(session)
    live = net_worth(session)

    assert result["total"] == live["total"]  # to the cent
    assert result["date"] == TODAY.isoformat()

    # one row per valued account, stamped today, at the current balance
    rows = session.execute(
        select(BalanceSnapshot).where(BalanceSnapshot.date == TODAY)
    ).scalars().all()
    assert len(rows) == 4
    assert round(sum(r.balance for r in rows), 2) == live["total"]


def test_today_history_row_includes_manual_accounts_never_touched_today(session):
    """The observed bug: a manual property valued days ago but not re-edited today
    dropped out of today's history total, opening a ~$1.4M gap vs get_accounts."""
    feed = _account(session, "Adv Plus Banking", 20_108.81)
    home = _account(session, "Home", 1_398_000.00, kind="property", source="manual")
    mortgage = _account(session, "Mortgage", -1_226_301.14, kind="mortgage", source="manual")

    # The manual accounts were last snapshotted five days ago; only the feed moves today.
    _snap(session, feed, 5, 20_108.81)
    _snap(session, home, 5, 1_398_000.00)
    _snap(session, mortgage, 5, -1_226_301.14)
    _snap(session, feed, 0, 20_108.81)

    history = net_worth_history(session)
    live = net_worth(session)

    # Today's point carries the property and mortgage forward, so it agrees with the
    # live accounts total instead of sagging by the property's whole value.
    assert history[-1]["date"] == TODAY.isoformat()
    assert history[-1]["total"] == live["total"]
    assert live["total"] == round(20_108.81 + 1_398_000.00 - 1_226_301.14, 2)


# --- test plan #2: intraday correction re-stamps today, nothing else moves ---

def test_rerun_snapshot_restamps_today_and_leaves_other_rows_untouched(session):
    feed = _account(session, "Adv Plus Banking", 12_000.00)
    mortgage = _account(session, "Mortgage", -5_000.00, kind="mortgage", source="manual")

    # A prior day's history that must survive the intraday re-stamp unchanged.
    _snap(session, feed, 3, 9_000.00)
    _snap(session, mortgage, 3, -5_000.00)

    snapshot_net_worth(session)
    assert net_worth_history(session)[-1]["total"] == 7_000.00  # 12000 - 5000

    # Correct the mortgage mid-day and re-stamp.
    mortgage.balance = -6_000.00
    session.flush()
    snapshot_net_worth(session)

    history = {p["date"]: p["total"] for p in net_worth_history(session)}
    assert history[TODAY.isoformat()] == 6_000.00  # 12000 - 6000, the corrected value
    assert history[(TODAY - timedelta(days=3)).isoformat()] == 4_000.00  # untouched

    # No duplicate today-rows: re-stamp updated in place.
    today_rows = session.execute(
        select(BalanceSnapshot).where(BalanceSnapshot.date == TODAY)
    ).scalars().all()
    assert len(today_rows) == 2
    # The three-days-ago rows are exactly as first written.
    old_rows = session.execute(
        select(BalanceSnapshot).where(BalanceSnapshot.date == TODAY - timedelta(days=3))
    ).scalars().all()
    assert sorted(round(r.balance, 2) for r in old_rows) == [-5_000.00, 9_000.00]


# --- test plan #3: a newly tracked account annotates a basis change, not a cliff ---

def test_new_manual_account_marks_basis_change_not_a_silent_step(session):
    feed = _account(session, "Adv Plus Banking", 10_000.00)
    for d in (4, 3, 2, 1, 0):
        _snap(session, feed, d, 10_000.00)

    # A property enters tracking two days ago and is snapshotted from then on.
    home = _account(session, "Home", 500_000.00, kind="property", source="manual")
    for d in (2, 1, 0):
        _snap(session, home, d, 500_000.00)

    points = {p["date"]: p for p in net_worth_history(session)}
    day2 = points[(TODAY - timedelta(days=2)).isoformat()]
    day3 = points[(TODAY - timedelta(days=3)).isoformat()]

    # The step at day-2 is flagged as a basis change, attributed to the new account,
    # so a chart can annotate it rather than draw a fake $490k jump.
    assert "basis_change" in day2
    entered = day2["basis_change"]["entered"]
    assert [e["name"] for e in entered] == ["Home"]
    assert day2["basis_change"]["amount"] == 500_000.00
    assert day2["total"] == 510_000.00

    # A day with no set change carries no annotation.
    assert "basis_change" not in day3
    assert day3["total"] == 10_000.00


# --- test plan #4: feed-only history is unchanged by the patch ---

def test_feed_only_history_is_unchanged_and_unannotated(session):
    a = _account(session, "Checking", 1_000.00)
    b = _account(session, "Savings", 2_000.00, kind="savings")
    for d in (3, 2, 1, 0):
        _snap(session, a, d, 1_000.00)
        _snap(session, b, d, 2_000.00)

    history = net_worth_history(session)

    # Same account set every day: totals are the plain per-date sums, keys are exactly
    # date+total, and nothing is annotated as a basis change.
    assert history == [
        {"date": (TODAY - timedelta(days=d)).isoformat(), "total": 3_000.00}
        for d in (3, 2, 1, 0)
    ]
    assert all(set(p) == {"date", "total"} for p in history)


def test_snapshot_is_idempotent_within_a_day(session):
    _account(session, "Checking", 1_000.00)
    snapshot_net_worth(session)
    snapshot_net_worth(session)
    rows = session.execute(
        select(BalanceSnapshot).where(BalanceSnapshot.date == TODAY)
    ).scalars().all()
    assert len(rows) == 1  # re-run overwrote, did not duplicate
