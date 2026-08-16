import pytest

from sqlalchemy import select

from bankai.connectors.csv_import import (
    import_csv,
    is_apple_card_export,
    parse_amount,
    parse_csv,
)
from bankai.models import Transaction

# Verbatim Apple Wallet export shape: charges POSITIVE, payments/refunds
# NEGATIVE — the opposite of every other export we ingest.
APPLE_CSV = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n"
    '08/01/2026,08/02/2026,"STARBUCKS 800-782-7282 WA",Starbucks,Restaurants,Purchase,6.75,Gaurav Anand\n'
    '07/25/2026,07/26/2026,"AMAZON MKTPL*DF2K27JH3 TERRY AVE N SEATTLE 98109 WA USA",Amazon,Shopping,Purchase,463.04,Gaurav Anand\n'
    '07/20/2026,07/21/2026,"UBER *EATS 1455 MARKET ST 94103 CA USA",Uber Eats,Restaurants,Refund,-17.52,Gaurav Anand\n'
    '07/15/2026,07/15/2026,"ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 1975",Apple Card,Payment,Payment,-1500.00,\n'
)


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


def test_apple_card_header_detected():
    assert is_apple_card_export(
        ["Transaction Date", "Clearing Date", "Description", "Merchant",
         "Category", "Type", "Amount (USD)", "Purchased By"]
    )
    assert not is_apple_card_export(["Date", "Description", "Amount"])
    assert not is_apple_card_export(["Posting Date", "Details", "Withdrawals", "Deposits"])


def test_apple_card_signs_inverted():
    txns = {t.description.split()[0]: t for t in parse_csv(APPLE_CSV)}
    assert txns["STARBUCKS"].amount == -6.75          # charge -> money out
    assert txns["AMAZON"].amount == -463.04
    assert txns["UBER"].amount == 17.52               # refund -> money back
    assert txns["ACH"].amount == 1500.00              # payment -> money in


def test_apple_card_import_categorizes_correctly(session):
    result = import_csv(session, text=APPLE_CSV, account_name="Apple Card", kind="credit")
    assert result.added == 4
    rows = {t.description.split()[0]: t for t in session.execute(select(Transaction)).scalars()}
    # A charge must never read as income — that was the live bug.
    assert rows["STARBUCKS"].amount < 0 and rows["STARBUCKS"].category != "income"
    assert rows["AMAZON"].category == "shopping"
    # The payment from checking stays money-in and lands in transfer,
    # so income summaries never see it.
    assert rows["ACH"].amount > 0 and rows["ACH"].category == "transfer"


def test_apple_card_reimport_is_idempotent(session):
    first = import_csv(session, text=APPLE_CSV, account_name="Apple Card", kind="credit")
    assert first.added == 4
    again = import_csv(session, text=APPLE_CSV, account_name="Apple Card", kind="credit")
    assert again.added == 0 and again.skipped == 4


def test_non_apple_csv_keeps_raw_signs():
    # Same amounts, generic header: no inversion may leak outside the Apple shape.
    text = (
        "Date,Description,Amount\n"
        "08/01/2026,STARBUCKS 800-782-7282 WA,-6.75\n"
        "07/15/2026,PAYCHECK,1500.00\n"
    )
    txns = parse_csv(text)
    assert txns[0].amount == -6.75
    assert txns[1].amount == 1500.00
