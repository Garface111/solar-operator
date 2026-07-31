import pytest

from bankai.connectors.csv_import import import_csv, parse_amount, parse_csv


def test_parse_amount_variants():
    assert parse_amount("$1,234.56") == 1234.56
    assert parse_amount("(45.00)") == -45.00
    assert parse_amount("-12.30") == -12.30
    assert parse_amount("") is None
    assert parse_amount("N/A") is None


def test_signed_amount_csv():
    text = (
        "Date,Description,Amount\n"
        "07/01/2026,HANNAFORD #8123,-54.20\n"
        "2026-07-02,ACME PAYROLL DIRECT DEP,2500.00\n"
    )
    txns = parse_csv(text)
    assert len(txns) == 2
    assert txns[0].amount == -54.20
    assert txns[1].posted.isoformat() == "2026-07-02"


def test_debit_credit_csv():
    text = (
        "Posting Date,Details,Withdrawals,Deposits\n"
        "07/03/2026,GMP ELECTRIC,120.00,\n"
        "07/05/2026,MOBILE DEPOSIT,,300.00\n"
    )
    txns = parse_csv(text)
    assert txns[0].amount == -120.00
    assert txns[1].amount == 300.00


def test_unrecognized_header_raises():
    with pytest.raises(ValueError):
        parse_csv("foo,bar\n1,2\n")


def test_import_csv_end_to_end_with_dedupe(session):
    text = "Date,Description,Amount\n07/01/2026,COFFEE,-4.00\n"
    first = import_csv(session, text=text, account_name="Card", kind="credit")
    assert first.added == 1
    second = import_csv(session, text=text, account_name="Card", kind="credit")
    assert second.added == 0 and second.skipped == 1
