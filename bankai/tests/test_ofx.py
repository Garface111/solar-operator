import pytest

from bankai.connectors.ofx_import import import_ofx, parse_ofx
from bankai.models import Account

SAMPLE_OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<SIGNONMSGSRSV1><SONRS>
<FI><ORG>Bank of America</ORG><FID>5959</FID></FI>
</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260715120000[0:GMT]
<TRNAMT>-54.20
<FITID>2026071501
<NAME>HANNAFORD #8123
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260716
<TRNAMT>2500.00
<FITID>2026071601
<NAME>ACME PAYROLL
<MEMO>DIRECT DEP
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>3123.45<DTASOF>20260717</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def test_parse_ofx_transactions_balance_org():
    txns, balance, institution = parse_ofx(SAMPLE_OFX)
    assert len(txns) == 2
    assert txns[0].posted.isoformat() == "2026-07-15"
    assert txns[0].amount == -54.20
    assert txns[0].external_id == "2026071501"
    assert txns[1].description == "ACME PAYROLL DIRECT DEP"
    assert balance == 3123.45
    assert institution == "Bank of America"


def test_import_ofx_sets_balance_and_dedupes(session):
    first = import_ofx(session, text=SAMPLE_OFX, account_name="BofA Checking")
    assert first.added == 2
    account = session.query(Account).filter_by(name="BofA Checking").one()
    assert account.balance == 3123.45
    assert account.institution == "Bank of America"
    again = import_ofx(session, text=SAMPLE_OFX, account_name="BofA Checking")
    assert again.added == 0 and again.skipped == 2


def test_non_ofx_raises():
    with pytest.raises(ValueError):
        parse_ofx("Date,Description,Amount\n07/01/2026,COFFEE,-4.00\n")
