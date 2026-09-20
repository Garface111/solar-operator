from datetime import date, datetime

from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.models import Rule
from bankai.rules.engine import evaluate_rules


def test_balance_below_fires_once_per_day(session):
    upsert_account(session, source="csv", name="Checking", balance=120.0)
    session.add(Rule(name="low bal", kind="balance_below", params={"threshold": 200}))
    session.flush()
    now = datetime(2026, 7, 29, 9, 0)
    first = evaluate_rules(session, now)
    assert len(first) == 1
    assert "120.00" in first[0].body
    again = evaluate_rules(session, datetime(2026, 7, 29, 15, 0))
    assert again == []  # same day → deduped
    next_day = evaluate_rules(session, datetime(2026, 7, 30, 9, 0))
    assert len(next_day) == 1


def test_balance_below_does_not_fire_above_threshold(session):
    upsert_account(session, source="csv", name="Checking", balance=500.0)
    session.add(Rule(name="low bal", kind="balance_below", params={"threshold": 200}))
    session.flush()
    assert evaluate_rules(session, datetime(2026, 7, 29)) == []


def test_reminder_fires_on_day_of_month_only(session):
    session.add(
        Rule(name="move savings", kind="reminder", params={"day_of_month": 25}, message="Move $500")
    )
    session.flush()
    assert evaluate_rules(session, datetime(2026, 7, 24)) == []
    fired = evaluate_rules(session, datetime(2026, 7, 25))
    assert len(fired) == 1 and "Move $500" in fired[0].body
    # same day again → deduped; next month fires again
    assert evaluate_rules(session, datetime(2026, 7, 25, 23)) == []
    assert len(evaluate_rules(session, datetime(2026, 8, 25))) == 1


def test_large_transaction_fires_per_txn_once(session):
    account = upsert_account(session, source="csv", name="Card", balance=0.0)
    ingest_transactions(
        session,
        account,
        [
            TxnIn(posted=date.today(), amount=-950.0, description="FANCY APPLIANCE STORE"),
            TxnIn(posted=date.today(), amount=-20.0, description="COFFEE"),
        ],
    )
    session.add(Rule(name="big spend", kind="large_transaction", params={"threshold": 500}))
    session.flush()
    fired = evaluate_rules(session, datetime.utcnow())
    assert len(fired) == 1
    assert "FANCY APPLIANCE" in fired[0].subject or "FANCY APPLIANCE" in fired[0].body
    assert evaluate_rules(session, datetime.utcnow()) == []


def test_weekly_digest_summarizes(session):
    account = upsert_account(session, source="csv", name="Checking", balance=100.0)
    ingest_transactions(
        session,
        account,
        [
            TxnIn(posted=date(2026, 7, 27), amount=-60.0, description="HANNAFORD GROCERY"),
            TxnIn(posted=date(2026, 7, 26), amount=2000.0, description="ACME PAYROLL"),
        ],
    )
    session.add(Rule(name="digest", kind="weekly_digest", params={"weekday": 3}))  # Thursday
    session.flush()
    assert evaluate_rules(session, datetime(2026, 7, 29)) == []  # Wednesday
    fired = evaluate_rules(session, datetime(2026, 7, 30))  # Thursday
    assert len(fired) == 1
    assert "Income: $2,000.00" in fired[0].body


def test_disabled_rule_never_fires(session):
    upsert_account(session, source="csv", name="Checking", balance=1.0)
    session.add(
        Rule(name="off", kind="balance_below", params={"threshold": 100}, enabled=False)
    )
    session.flush()
    assert evaluate_rules(session, datetime(2026, 7, 29)) == []
