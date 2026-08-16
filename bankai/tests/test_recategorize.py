"""The copilot's own ask (act_88417bc588ea4ce8): own-card payments are
transfers, the mortgage is the mortgage — and the fix must survive the next
sync, not just rewrite last week."""
import json
from datetime import date

from bankai.agent.tools import execute_tool
from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.intelligence.categorize import categorize
from bankai.intelligence.insights import spending_summary
from bankai.models import CategoryRule, Transaction


def _result(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def _seed(session):
    acct = upsert_account(session, source="csv", name="Checking 2724")
    ingest_transactions(session, acct, [
        TxnIn(date(2026, 8, 3), -6000.0, "Mobile Banking payment to CRD 1420"),
        TxnIn(date(2026, 8, 4), -2500.0, "Mobile Banking payment to CRD 5970"),
        TxnIn(date(2026, 8, 5), -9270.40, "JPMORGAN CHASE DES:CHASE ACH"),
        TxnIn(date(2026, 8, 5), -84.0, "HANNAFORD GROCERY"),
    ])
    return acct


def test_house_rules_outrank_the_keyword_table():
    rules = (("Mobile Banking payment to CRD", "transfer"),)
    assert categorize("Mobile Banking payment to CRD 1420", -6000, rules) == "transfer"
    # without the rule, whatever the table guesses, it is not transfer
    assert categorize("Mobile Banking payment to CRD 1420", -6000) != "transfer"


def test_recategorize_updates_history_and_saves_a_standing_rule(session):
    acct = _seed(session)
    out = _result(execute_tool(session, "recategorize_transactions", {
        "description_contains": "Mobile Banking payment to CRD",
        "new_category": "transfer",
        "reason": "the household paying its own cards is not spending",
    }))
    assert out["updated"] == 2 and out["rule_saved"] is True

    out2 = _result(execute_tool(session, "recategorize_transactions", {
        "description_contains": "JPMORGAN CHASE DES:CHASE ACH",
        "new_category": "mortgage_rent",
    }))
    assert out2["updated"] == 1

    # history is fixed: the card payments are out of the spend buckets, so the
    # window's spend is groceries + mortgage, not $17,854 of self-transfers
    summary = spending_summary(session, date(2026, 8, 1), date(2026, 8, 8))
    assert not any("transfer" in str(c) for c in summary["by_category"])
    assert abs(summary["spend"]) == 9354.40  # 9270.40 mortgage + 84 groceries

    # and the NEXT sync labels a fresh card payment correctly — the real fix
    result = ingest_transactions(session, acct, [
        TxnIn(date(2026, 8, 20), -3000.0, "Mobile Banking payment to CRD 1420"),
    ])
    fresh = session.get(Transaction, result.ids[0])
    assert fresh.category == "transfer"


def test_recategorize_refuses_broad_or_empty_selectors(session):
    _seed(session)
    out = _result(execute_tool(session, "recategorize_transactions",
                               {"new_category": "transfer"}))
    assert "refusing" in out["error"]
    out = _result(execute_tool(session, "recategorize_transactions",
                               {"description_contains": "CRD", "new_category": "transfer"}))
    assert "at least 4" in out["error"]


def test_recategorize_by_ids_does_not_save_a_rule(session):
    _seed(session)
    txn = session.execute(
        Transaction.__table__.select().where(Transaction.description.like("%HANNAFORD%"))
    ).fetchone()
    out = _result(execute_tool(session, "recategorize_transactions", {
        "transaction_ids": [txn.id], "new_category": "dining",
    }))
    assert out["updated"] == 1 and out["rule_saved"] is False
    assert session.query(CategoryRule).count() == 0
