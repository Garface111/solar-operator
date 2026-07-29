from datetime import date

from bankai.ingest import TxnIn, ingest_transactions, normalize_desc, upsert_account


def _acct(session, name="Joint Checking"):
    return upsert_account(session, source="csv", name=name, balance=1000.0)


def test_reimport_is_noop(session):
    account = _acct(session)
    txns = [
        TxnIn(posted=date(2026, 7, 1), amount=-42.50, description="HANNAFORD #123"),
        TxnIn(posted=date(2026, 7, 2), amount=2500.00, description="ACME PAYROLL DIRECT DEP"),
    ]
    first = ingest_transactions(session, account, txns)
    assert first.added == 2
    second = ingest_transactions(session, account, txns)
    assert second.added == 0 and second.skipped == 2


def test_same_day_identical_duplicates_both_kept(session):
    account = _acct(session)
    txns = [
        TxnIn(posted=date(2026, 7, 3), amount=-4.75, description="COFFEE SHOP"),
        TxnIn(posted=date(2026, 7, 3), amount=-4.75, description="COFFEE SHOP"),
    ]
    result = ingest_transactions(session, account, txns)
    assert result.added == 2
    again = ingest_transactions(session, account, txns)
    assert again.added == 0


def test_external_id_wins_over_description_changes(session):
    account = _acct(session)
    a = TxnIn(posted=date(2026, 7, 4), amount=-10.0, description="PENDING FOO", external_id="e1")
    ingest_transactions(session, account, [a])
    b = TxnIn(posted=date(2026, 7, 4), amount=-10.0, description="SETTLED FOO", external_id="e1")
    result = ingest_transactions(session, account, [b])
    assert result.added == 0 and result.skipped == 1


def test_normalize_strips_reference_numbers():
    assert normalize_desc("AMAZON.COM*1A2B34567 REF 99881") == normalize_desc(
        "AMAZON.COM*9Z8Y77777 REF 11223"
    )


def test_upsert_account_updates_balance_and_snapshots(session):
    account = _acct(session)
    assert account.balance == 1000.0
    again = upsert_account(session, source="csv", name="Joint Checking", balance=900.0)
    assert again.id == account.id
    assert again.balance == 900.0
