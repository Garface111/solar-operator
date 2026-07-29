from sqlalchemy import select

from bankai.ingest import (
    MANUAL_KINDS,
    normalize_manual_balance,
    upsert_account,
)
from bankai.intelligence.insights import net_worth
from bankai.models import BalanceSnapshot


def test_liabilities_entered_as_owed_become_negative():
    assert normalize_manual_balance("mortgage", 1_160_000) == -1_160_000
    assert normalize_manual_balance("mortgage", -1_160_000) == -1_160_000
    assert normalize_manual_balance("loan", 5000) == -5000
    assert normalize_manual_balance("property", 1_460_000) == 1_460_000
    assert normalize_manual_balance("property", -1_460_000) == 1_460_000
    assert normalize_manual_balance("other", -250) == -250
    assert normalize_manual_balance("other", 250) == 250


def test_home_equity_flows_into_net_worth(session):
    upsert_account(
        session, source="manual", name="Home — 36001 Cabrillo Dr",
        kind="property", balance=normalize_manual_balance("property", 1_460_000),
    )
    upsert_account(
        session, source="manual", name="Mortgage — 36001 Cabrillo Dr",
        kind="mortgage", balance=normalize_manual_balance("mortgage", 1_160_000),
    )
    nw = net_worth(session)
    assert nw["total"] == 300_000.0
    by_name = {a["name"]: a for a in nw["accounts"]}
    assert by_name["Home — 36001 Cabrillo Dr"]["source"] == "manual"
    assert by_name["Mortgage — 36001 Cabrillo Dr"]["balance"] == -1_160_000.0


def test_manual_upsert_updates_and_snapshots(session):
    a1 = upsert_account(
        session, source="manual", name="Mortgage", kind="mortgage", balance=-1_160_000.0
    )
    a2 = upsert_account(
        session, source="manual", name="Mortgage", kind="mortgage", balance=-1_150_000.0
    )
    assert a1.id == a2.id
    assert a2.balance == -1_150_000.0
    snaps = session.execute(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == a1.id)
    ).scalars().all()
    assert len(snaps) == 1  # same day: snapshot overwritten, not duplicated
    assert snaps[0].balance == -1_150_000.0


def test_manual_kinds_are_the_ui_contract():
    assert MANUAL_KINDS == ["property", "vehicle", "other", "mortgage", "loan"]


# --- the copilot correcting its own stale data ---

def test_agent_can_correct_a_manual_balance(session):
    import json
    from bankai.agent.tools import execute_tool
    from bankai.models import MemoryNote

    mortgage = upsert_account(
        session, source="manual", name="Mortgage — 36001 Cabrillo Dr",
        kind="mortgage", balance=-1_160_000.0,
    )
    session.flush()
    out = json.loads(execute_tool(session, "update_account_balance", {
        "account_id": mortgage.id,
        "balance": 1_226_301.14,  # entered as owed
        "reasoning": "Chase statement 07/01/2026, acct #1489812312: unpaid principal.",
    }))
    assert out["updated"]
    assert out["old_balance"] == -1_160_000.0
    assert out["new_balance"] == -1_226_301.14  # stored negative
    assert mortgage.balance == -1_226_301.14
    # the reasoning survives as a note, and the change is in balance history
    note = session.query(MemoryNote).filter(
        MemoryNote.title.like("Balance basis%")
    ).one()
    assert "Chase statement" in note.content
    snaps = session.execute(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == mortgage.id)
    ).scalars().all()
    assert any(s.balance == -1_226_301.14 for s in snaps)


def test_agent_cannot_edit_a_bank_synced_balance(session):
    """The institution is the truth for synced accounts — the copilot must not
    be able to paper over a feed with a number it likes better."""
    import json
    from bankai.agent.tools import execute_tool

    checking = upsert_account(
        session, source="simplefin", name="Adv Plus Banking", kind="checking",
        balance=20_108.81,
    )
    session.flush()
    out = json.loads(execute_tool(session, "update_account_balance", {
        "account_id": checking.id, "balance": 99_999.0, "reasoning": "wishful",
    }))
    assert "error" in out and "simplefin" in out["error"]
    assert checking.balance == 20_108.81


def test_updating_an_unknown_account_errors_clearly(session):
    import json
    from bankai.agent.tools import execute_tool

    out = json.loads(execute_tool(session, "update_account_balance", {
        "account_id": "acct_nope", "balance": 1.0, "reasoning": "x",
    }))
    assert "error" in out
