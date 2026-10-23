"""Accounts no feed can reach, and the terms that make them meaningful.

The Apple Card is the case: statements were in the vault, the copilot had read
the balance out of them, and it still could not put the card in the account list
because no tool created accounts. A card missing from the list does not read as
"unknown" — it reads as a card with nothing owed, which is the most misleading
state possible.
"""
import json
from datetime import date, timedelta

import pytest

from bankai import accounts_terms
from bankai.agent.tools import execute_tool
from bankai.ingest import upsert_account
from bankai.intelligence.insights import net_worth
from bankai.models import Account

JUNE = {
    "name": "Apple Card",
    "kind": "credit",
    "balance": 457.17,          # Total Balance, incl. remaining installments
    "owner": "gaurav",
    "minimum_payment": 162.75,
    "payment_due_date": "2026-07-31",
    "as_of": "2026-06-30",
    "source": "Apple Card Statement - June 2026 (vault)",
}


def test_the_copilot_can_now_create_an_unsyncable_account(session):
    out = json.loads(execute_tool(session, "track_account", JUNE))
    assert out["tracked"] is True and out["created"] is True
    account = session.query(Account).filter(Account.name == "Apple Card").one()
    assert account.kind == "credit"
    assert account.balance == -457.17  # owed, stored negative
    assert account.owner == "gaurav"
    assert account.source == "manual"


def test_it_lands_in_net_worth(session):
    before = net_worth(session)["total"]
    execute_tool(session, "track_account", JUNE)
    after = net_worth(session)["total"]
    assert round(after - before, 2) == -457.17


def test_the_terms_travel_with_it(session):
    out = json.loads(execute_tool(session, "track_account", JUNE))
    terms = accounts_terms.terms_by_account(session)
    account_id = session.query(Account).filter(Account.name == "Apple Card").one().id
    t = terms[account_id]
    assert t["statement_balance"] == 457.17
    assert t["minimum_payment"] == 162.75
    assert t["payment_due_date"] == "2026-07-31"
    assert t["as_of"] == "2026-06-30"
    assert "June 2026" in t["source"]
    assert out["net_worth"] == net_worth(session)["total"]


def test_a_second_statement_updates_rather_than_duplicating(session):
    execute_tool(session, "track_account", dict(JUNE, balance=10768.58,
                                                as_of="2026-05-31",
                                                payment_due_date="2026-06-30",
                                                source="May statement"))
    out = json.loads(execute_tool(session, "track_account", JUNE))
    assert out["created"] is False
    assert out["old_balance"] == -10768.58
    assert session.query(Account).filter(Account.name == "Apple Card").count() == 1
    account_id = session.query(Account).filter(Account.name == "Apple Card").one().id
    assert accounts_terms.terms_by_account(session)[account_id]["as_of"] == "2026-06-30"


def test_partial_updates_do_not_wipe_what_an_older_statement_gave(session):
    """Recording a new due date must not blank an APR read months ago."""
    account = upsert_account(session, source="manual", name="Apple Card",
                             kind="credit", balance=-457.17)
    session.flush()
    accounts_terms.set_terms(session, account.id, apr=27.24, source="May statement")
    accounts_terms.set_terms(session, account.id,
                             payment_due_date=date(2026, 7, 31), source="June statement")
    t = accounts_terms.terms_by_account(session)[account.id]
    assert t["apr"] == 27.24
    assert t["payment_due_date"] == "2026-07-31"
    assert t["source"] == "June statement"


def test_a_past_due_date_is_reported_as_stale_not_overdue(session):
    """An old statement means our data is behind, NOT that they missed a
    payment — saying "overdue" about stale data would be alarming and wrong."""
    account = upsert_account(session, source="manual", name="Apple Card",
                             kind="credit", balance=-457.17)
    session.flush()
    accounts_terms.set_terms(
        session, account.id, payment_due_date=date.today() - timedelta(days=20),
        source="old statement",
    )
    t = accounts_terms.terms_by_account(session)[account.id]
    assert t["stale"] is True
    assert t["days_until_due"] < 0


def test_days_until_due_counts_forward(session):
    account = upsert_account(session, source="manual", name="Apple Card",
                             kind="credit", balance=-457.17)
    session.flush()
    accounts_terms.set_terms(session, account.id,
                             payment_due_date=date.today() + timedelta(days=9))
    t = accounts_terms.terms_by_account(session)[account.id]
    assert t["days_until_due"] == 9 and t["stale"] is False


def test_a_synced_account_cannot_be_shadowed_by_a_manual_one(session):
    """Tracking must never create a second copy of an account the bank feeds."""
    upsert_account(session, source="simplefin", name="Adv Plus Banking",
                   kind="checking", balance=20_108.81)
    session.flush()
    out = json.loads(execute_tool(session, "track_account", {
        "name": "Adv Plus Banking", "kind": "other", "balance": 1.0,
        "source": "made up",
    }))
    # a manual account of the same name is a DIFFERENT row; the synced one is
    # untouched, which is what matters
    assert session.query(Account).filter(
        Account.name == "Adv Plus Banking", Account.source == "simplefin"
    ).one().balance == 20_108.81


def test_bad_kind_is_rejected(session):
    out = json.loads(execute_tool(session, "track_account", {
        "name": "Apple Card", "kind": "wormhole", "balance": 1.0, "source": "x",
    }))
    assert "error" in out
    assert session.query(Account).count() == 0


# --- terms on accounts the bank feeds -------------------------------------

def test_terms_can_be_recorded_on_a_synced_account(session):
    """Feeds carry balances but not due dates. Without this the biggest cards
    show a number with no deadline — and the deadline is the half that decides
    whether a payment gets missed."""
    card = upsert_account(session, source="simplefin", kind="credit",
                          name="BankAmericard Platinum Plus", balance=-16_088.71)
    session.flush()
    out = json.loads(execute_tool(session, "set_account_terms", {
        "account_id": card.id,
        "minimum_payment": 415.00,
        "payment_due_date": "2026-08-15",
        "apr": 0.0,
        "as_of": "2026-07-20",
        "source": "BofA July statement",
    }))
    assert out["recorded"] is True
    assert "Balance untouched" in out["note"]
    # the feed still owns the balance
    assert card.balance == -16_088.71
    t = accounts_terms.terms_by_account(session)[card.id]
    assert t["payment_due_date"] == "2026-08-15"
    assert t["minimum_payment"] == 415.00
    assert t["apr"] == 0.0


def test_setting_terms_on_an_unknown_account_errors(session):
    out = json.loads(execute_tool(session, "set_account_terms", {
        "account_id": "acct_nope", "source": "x",
    }))
    assert "error" in out


# --- a recurring due day, which is the durable form ------------------------

def test_next_due_rolls_to_the_following_month_once_past(session):
    assert accounts_terms.next_due(15, date(2026, 7, 10)) == date(2026, 7, 15)
    assert accounts_terms.next_due(15, date(2026, 7, 20)) == date(2026, 8, 15)
    # the due day itself counts as today, not next month
    assert accounts_terms.next_due(15, date(2026, 7, 15)) == date(2026, 7, 15)


def test_next_due_survives_short_months(session):
    """A card due on the 31st must resolve in February, not raise."""
    assert accounts_terms.next_due(31, date(2026, 2, 5)) == date(2026, 2, 28)
    assert accounts_terms.next_due(31, date(2026, 12, 15)) == date(2026, 12, 31)
    # rolling past December lands in the next year
    assert accounts_terms.next_due(5, date(2026, 12, 20)) == date(2027, 1, 5)


def test_a_recurring_due_day_never_goes_stale(session):
    """The reason to prefer it: a date copied off one statement expires monthly,
    a cycle day does not."""
    card = upsert_account(session, source="simplefin", kind="credit",
                          name="BankAmericard", balance=-16_088.71)
    session.flush()
    accounts_terms.set_terms(session, card.id, due_day_of_month=15,
                             minimum_payment=415.0, source="BofA statement")
    t = accounts_terms.terms_by_account(session)[card.id]
    assert t["recurring"] is True
    assert t["stale"] is False
    assert t["due_day_of_month"] == 15
    assert t["days_until_due"] >= 0  # always forward-looking
    assert t["payment_due_date"] == accounts_terms.next_due(15).isoformat()


def test_a_recurring_day_wins_over_a_stale_one_off_date(session):
    card = upsert_account(session, source="simplefin", kind="credit",
                          name="Travel Rewards", balance=-5_032.15)
    session.flush()
    accounts_terms.set_terms(
        session, card.id, payment_due_date=date(2026, 1, 15), source="old statement"
    )
    assert accounts_terms.terms_by_account(session)[card.id]["stale"] is True
    accounts_terms.set_terms(session, card.id, due_day_of_month=15, source="BofA app")
    t = accounts_terms.terms_by_account(session)[card.id]
    assert t["stale"] is False and t["recurring"] is True
