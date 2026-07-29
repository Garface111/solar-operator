"""An emailed file has to reach the right place — the vault, or the ledger.

The Apple Card is why this exists: no aggregator can reach it, so a Wallet export
sent by hand is the ONLY way its transactions ever arrive. Filing that as a
document would leave the household looking at a saved file while their balances
stayed wrong.
"""
import base64

import pytest

from bankai import config, vault
from bankai.connectors import attachments
from bankai.messaging import email_thread
from bankai.models import Account, Document, Transaction

APPLE_CSV = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)\n"
    "07/02/2026,07/03/2026,APPLE STORE,Apple,Shopping,Purchase,129.99\n"
    "07/11/2026,07/12/2026,WHOLE FOODS,Whole Foods,Grocery,Purchase,84.20\n"
    "07/19/2026,07/20/2026,ACME UTILITIES,Acme,Utilities,Purchase,212.40\n"
)


@pytest.fixture(autouse=True)
def documents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DOCUMENTS_DIR", tmp_path / "documents")


# --- routing ---

def test_recognises_data_files():
    assert attachments.looks_like_data("statement.csv")
    assert attachments.looks_like_data("Export.QFX")
    assert not attachments.looks_like_data("deed.pdf")
    assert not attachments.looks_like_data("policy.docx")


def test_account_is_only_guessed_when_unmistakable():
    assert attachments.guess_account("apple-card-2026-07.csv", "") == ("Apple Card", "credit")
    assert attachments.guess_account("export.csv", "Apple Card July") == ("Apple Card", "credit")
    assert attachments.guess_account("amex_activity.csv", "") == ("Amex", "credit")
    # a bare filename says nothing — guessing would split a card's history
    assert attachments.guess_account("transactions.csv", "here you go") is None


# --- the Apple Card path, end to end ---

def test_apple_card_export_becomes_real_transactions(session):
    out = attachments.handle(
        session, filename="apple-card-july.csv", data=APPLE_CSV.encode(),
        sender="Gaurav", subject="Apple Card export",
    )
    assert out["imported"] is True
    assert out["account"] == "Apple Card"
    assert out["added"] == 3

    account = session.query(Account).filter(Account.name == "Apple Card").one()
    assert account.kind == "credit"
    assert session.query(Transaction).filter(Transaction.account_id == account.id).count() == 3

    # and the original is still on file, with what happened to it
    doc = session.query(Document).one()
    assert "Gaurav" in doc.summary and "Apple Card" in doc.summary
    assert "3 transactions added" in doc.summary


def test_re_sending_the_same_export_adds_nothing(session):
    attachments.handle(session, filename="apple-card-july.csv", data=APPLE_CSV.encode(),
                       sender="Gaurav", subject="Apple Card")
    again = attachments.handle(session, filename="apple-card-july.csv", data=APPLE_CSV.encode(),
                               sender="Gaurav", subject="Apple Card")
    assert again["added"] == 0
    assert again["duplicates_skipped"] == 3
    assert session.query(Transaction).count() == 3


def test_an_unidentifiable_statement_asks_instead_of_guessing(session):
    out = attachments.handle(
        session, filename="transactions.csv", data=APPLE_CSV.encode(),
        sender="Gaurav", subject="here you go",
    )
    assert out["imported"] is False
    assert out["needs_account"] is True
    assert "do not guess" in out["note"].lower()
    assert session.query(Account).count() == 0  # no phantom account invented
    assert session.query(Document).count() == 1  # but the file is kept


def test_an_unreadable_file_is_reported_not_swallowed(session):
    out = attachments.handle(
        session, filename="apple-card.csv", data=b"\x00\x01 not a statement at all",
        sender="Gaurav", subject="Apple Card",
    )
    assert out["imported"] is False
    assert "import_error" in out
    assert session.query(Document).count() == 1


def test_a_document_is_filed_and_not_treated_as_data(session):
    out = attachments.handle(
        session, filename="Family_Trust_Agreement.pdf", data=b"%PDF trust",
        sender="Ford", subject="the trust", category="estate",
    )
    assert out["filed"] is True and out["imported"] is False
    assert session.query(Document).one().category == "estate"


# --- what the copilot is told ---

def test_the_turn_is_told_exactly_what_happened_to_each_file():
    text = email_thread._describe_attachments([
        {"filename": "apple.csv", "imported": True, "account": "Apple Card",
         "added": 3, "duplicates_skipped": 0},
        {"filename": "mystery.csv", "needs_account": True},
        {"filename": "broken.csv", "import_error": "no date column"},
        {"filename": "deed.pdf", "document_id": "doc_1"},
    ])
    assert "IMPORTED into 'Apple Card'" in text
    assert "ASK which account" in text
    assert "could NOT be read" in text
    assert "annotate_document" in text


def test_an_email_with_only_an_attachment_still_gets_answered(session, monkeypatch):
    """Someone forwarding a statement often writes nothing. Silence plus a file
    is still a message — answering nothing would look broken."""
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", "Ford:f@x.com,Gaurav:g@x.com")
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: "ok")
    seen = {}
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": seen.setdefault("h", history) and "" or "Got it.",
    )
    reply = email_thread.process_message(session, {
        "from": "g@x.com", "subject": "", "message_id": "<a@b>", "references": "",
        "body": "",
        "attachments": [{"filename": "apple.csv", "imported": True,
                         "account": "Apple Card", "added": 3, "duplicates_skipped": 0}],
    })
    assert reply == "Got it."
    assert "IMPORTED into 'Apple Card'" in seen["h"][-1]["content"]


def test_a_csv_import_admits_it_has_no_balance(session):
    """A CSV lists activity without the amount outstanding. Left silent, the card
    sits at zero and understates what the household owes."""
    out = attachments.handle(
        session, filename="apple-card-july.csv", data=APPLE_CSV.encode(),
        sender="Gaurav", subject="Apple Card",
    )
    assert out["imported"] is True
    assert out["balance_unknown"] is True
    assert "contributes nothing to net worth" in out["note"]
    assert session.query(Account).filter(Account.name == "Apple Card").one().balance is None

    described = email_thread._describe_attachments([out])
    assert "BUT:" in described and "OFX/QFX" in described


def test_an_ofx_import_carries_a_balance_and_says_nothing_extra(session):
    ofx = (
        "OFXHEADER:100\n<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>"
        "<CURDEF>USD</CURDEF><CCACCTFROM><ACCTID>1111</ACCTID></CCACCTFROM>"
        "<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260702</DTPOSTED>"
        "<TRNAMT>-129.99</TRNAMT><FITID>a1</FITID><NAME>APPLE STORE</NAME></STMTTRN>"
        "</BANKTRANLIST><LEDGERBAL><BALAMT>-1543.21</BALAMT><DTASOF>20260729</DTASOF>"
        "</LEDGERBAL></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"
    )
    out = attachments.handle(
        session, filename="apple-card.qfx", data=ofx.encode(),
        sender="Gaurav", subject="Apple Card",
    )
    assert out["imported"] is True
    assert not out.get("balance_unknown")
    assert session.query(Account).filter(Account.name == "Apple Card").one().balance == -1543.21
