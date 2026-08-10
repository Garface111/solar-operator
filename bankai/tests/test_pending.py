"""Mentioned money vs. real data: the pending-expense ledger and its matcher.

The Apple Card is the reason all of this exists — no feed, data arrives by
emailed Wallet export weeks late — so the tests speak Apple Card throughout.
"""
import json
from datetime import date, timedelta

import pytest

from bankai import pending, vault
from bankai.agent import chat as agent_chat
from bankai.agent.tools import execute_tool
from bankai.connectors import attachments
from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.messaging import email_thread
from bankai.models import PendingExpense, Transaction


@pytest.fixture(autouse=True)
def documents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DOCUMENTS_DIR", tmp_path / "documents")


def make_card(session, name="Apple Card"):
    account = upsert_account(session, source="manual", name=name, kind="credit")
    session.flush()
    return account


def post(session, account, amount, description, posted):
    result = ingest_transactions(
        session, account, [TxnIn(posted=posted, amount=amount, description=description)]
    )
    return result.ids[0]


# --- noting mentions ---

def test_a_mention_is_stored_negative_and_deduped(session):
    row, created = pending.note(
        session, amount=840, description="New tires", account_hint="Apple Card",
        mentioned_on=date(2026, 8, 10),
    )
    assert created and row.amount == -840.0 and row.status == "open"
    # the household talking about the same spend again is not a second spend
    again, created2 = pending.note(
        session, amount=840, description="the tires", account_hint="Apple Card",
        mentioned_on=date(2026, 8, 12),
    )
    assert not created2 and again.id == row.id
    assert session.query(PendingExpense).count() == 1


def test_open_items_report_age_and_staleness(session):
    pending.note(session, amount=50, description="old thing",
                 mentioned_on=date(2026, 6, 1))
    items = pending.open_items(session, today=date(2026, 8, 10))
    assert items[0]["age_days"] == 70 and items[0]["stale"] is True


# --- the matcher ---

def test_a_rounded_mention_matches_the_posted_amount(session):
    card = make_card(session)
    pending.note(session, amount=840, description="New tires",
                 account_hint="Apple Card", mentioned_on=date(2026, 8, 1))
    txn_id = post(session, card, -838.60, "COSTCO TIRE CENTER", date(2026, 8, 3))
    matches = pending.reconcile(session, [txn_id])
    assert len(matches) == 1
    row = session.query(PendingExpense).one()
    assert row.status == "matched" and row.matched_transaction_id == txn_id


def test_an_unrelated_amount_does_not_match(session):
    card = make_card(session)
    pending.note(session, amount=840, description="New tires",
                 account_hint="Apple Card", mentioned_on=date(2026, 8, 1))
    txn_id = post(session, card, -19.99, "NETFLIX", date(2026, 8, 3))
    assert pending.reconcile(session, [txn_id]) == []
    assert session.query(PendingExpense).one().status == "open"


def test_a_transaction_confirms_at_most_one_mention(session):
    card = make_card(session)
    pending.note(session, amount=60, description="groomer, they said",
                 mentioned_on=date(2026, 7, 20))
    # second identical mention outside the dedupe window
    pending.note(session, amount=60, description="groomer again",
                 mentioned_on=date(2026, 8, 8))
    txn_id = post(session, card, -60.0, "PAWS & CLAWS", date(2026, 8, 1))
    matches = pending.reconcile(session, [txn_id])
    assert len(matches) == 1  # oldest mention wins, the other stays open
    statuses = sorted(r.status for r in session.query(PendingExpense).all())
    assert statuses == ["matched", "open"]


def test_the_hint_keeps_a_match_on_the_right_account(session):
    other = make_card(session, name="Amex")
    pending.note(session, amount=120, description="dinner",
                 account_hint="Apple Card", mentioned_on=date(2026, 8, 1))
    txn_id = post(session, other, -120.0, "RESTAURANT", date(2026, 8, 2))
    assert pending.reconcile(session, [txn_id]) == []  # wrong card, no match


def test_a_charge_far_outside_the_window_stays_unmatched(session):
    card = make_card(session)
    pending.note(session, amount=200, description="something",
                 mentioned_on=date(2026, 8, 1))
    txn_id = post(session, card, -200.0, "OLD CHARGE", date(2026, 4, 1))
    assert pending.reconcile(session, [txn_id]) == []


# --- through the tools ---

def test_note_and_list_via_tools(session):
    out = json.loads(execute_tool(session, "note_pending_expense", {
        "amount": 840, "description": "New tires", "account": "Apple Card",
        "date": "2026-08-10", "spender": "Gaurav",
    }))
    assert out["noted"] is True and out["open_pending_count"] == 1
    dup = json.loads(execute_tool(session, "note_pending_expense", {
        "amount": 840, "description": "tires again", "account": "Apple Card",
        "date": "2026-08-11",
    }))
    assert dup["noted"] is False and dup["duplicate_of"] == out["pending_id"]
    listed = json.loads(execute_tool(session, "list_pending_expenses", {}))
    assert len(listed["open"]) == 1
    assert listed["total_open_amount"] == -840.0


# --- the full circle: mention -> statement import -> confirmed ---

APPLE_CSV = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)\n"
    "08/03/2026,08/04/2026,COSTCO TIRE CENTER,Costco,Shopping,Purchase,838.60\n"
    "08/05/2026,08/06/2026,WHOLE FOODS,Whole Foods,Grocery,Purchase,84.20\n"
)


def test_statement_import_settles_the_mention_and_tells_the_turn(session):
    make_card(session)
    pending.note(session, amount=840, description="New tires",
                 account_hint="Apple Card", mentioned_on=date(2026, 8, 1))
    out = attachments.handle(
        session, filename="apple-card-aug.csv", data=APPLE_CSV.encode(),
        sender="Gaurav", subject="Apple Card export",
    )
    assert out["imported"] is True
    assert len(out["pending_matched"]) == 1
    assert session.query(PendingExpense).one().status == "matched"

    described = email_thread._describe_attachments([out])
    assert "CONFIRMED a spend the household had mentioned" in described
    assert "New tires" in described

    # re-importing the same export must not resurrect or re-match anything
    again = attachments.handle(
        session, filename="apple-card-aug.csv", data=APPLE_CSV.encode(),
        sender="Gaurav", subject="Apple Card export",
    )
    assert again["added"] == 0 and "pending_matched" not in again


# --- doctrine ---

def test_every_channel_knows_the_apple_card_lag(session):
    system = agent_chat.build_system(session, channel="web")
    assert "Apple Card" in system and "list_pending_expenses" in system


def test_whatsapp_knows_the_three_homes_for_a_spend(session):
    system = agent_chat.build_system(session, channel="whatsapp")
    assert "note_pending_expense" in system
    assert "log_expense" in system
