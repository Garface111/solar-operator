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
