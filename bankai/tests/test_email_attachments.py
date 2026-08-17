"""Emailing the actual files.

The copilot assembled a complete estate packet for the household's attorney and
then had to write: "I cannot attach vault files to an email. I have no tool that
does it." It was right, and it filed a code proposal instead of pretending. This
is that proposal, built.
"""
import json

import pytest

from bankai import config, vault
from bankai.agent.tools import execute_tool
from bankai.connectors import email_harvest
from bankai.messaging import email_thread
from bankai.models import ChatMessage

FORD = "ford@example.test"
GAURAV = "gaurav@example.test"


@pytest.fixture(autouse=True)
def household(monkeypatch, tmp_path):
    monkeypatch.setattr(vault, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", f"Ford:{FORD},Gaurav:{GAURAV}")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", "copilot@example.test")


def add(session, filename, data, title=None, category="estate"):
    doc, _ = vault.add_document(session, filename=filename, data=data,
                                title=title, category=category)
    session.flush()
    return doc


# --- the transport ---

def test_resend_carries_files_as_base64(monkeypatch):
    import base64
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        email_harvest.httpx, "post",
        lambda url, headers=None, json=None, timeout=None:
            captured.update(json) or FakeResponse(),
    )
    email_harvest.send_message(
        to=[FORD], subject="packet", text="see attached",
        attachments=[{"filename": "trust.pdf", "data": b"%PDF trust bytes"}],
    )
    sent = captured["attachments"][0]
    assert sent["filename"] == "trust.pdf"
    assert base64.b64decode(sent["content"]) == b"%PDF trust bytes"


def test_an_oversized_packet_is_refused_not_silently_trimmed(monkeypatch):
    """Dropping a file from a packet someone is about to send their attorney,
    without saying so, is the worst possible outcome."""
    monkeypatch.setattr(email_harvest, "MAX_ATTACHMENT_TOTAL_BYTES", 1000)
    with pytest.raises(RuntimeError, match="over the"):
        email_harvest.send_message(
            to=[FORD], subject="s", text="t",
            attachments=[{"filename": "big.pdf", "data": b"x" * 1200}],
        )


# --- gathering from the vault ---

def test_documents_are_loaded_with_their_real_bytes(session):
    doc = add(session, "trust.pdf", b"%PDF the operative instrument", title="Trust")
    attachments, notes = email_thread.collect_documents(session, [doc.id])
    assert notes == []
    assert attachments[0]["data"] == b"%PDF the operative instrument"
    assert attachments[0]["title"] == "Trust"


def test_a_missing_file_is_reported_rather_than_skipped(session):
    doc = add(session, "gone.pdf", b"%PDF temporary")
    path = vault.stored_path(doc)
    path.unlink()
    attachments, notes = email_thread.collect_documents(session, [doc.id, "doc_nope"])
    assert attachments == []
    problems = {n["problem"] for n in notes}
    assert "original file is missing from disk" in problems
    assert "no such document" in problems


def test_a_social_security_number_is_flagged(session):
    clean = add(session, "deed.pdf", b"%PDF a deed with no personal identifiers")
    exposed = add(session, "return.txt", b"Taxpayer SSN 123-45-6789 filing jointly",
                  category="tax")
    attachments, _ = email_thread.collect_documents(session, [clean.id, exposed.id])
    flags = {a["title"]: a["has_ssn"] for a in attachments}
    assert flags[exposed.title] is True
    assert flags[clean.title] is False


# --- sending ---

def test_the_packet_goes_to_both_spouses_with_files_attached(session, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        email_harvest, "send_message", lambda **kw: sent.update(kw) or "sent via Resend"
    )
    monkeypatch.setattr(email_thread.email_harvest, "send_message",
                        lambda **kw: sent.update(kw) or "sent via Resend")
    trust = add(session, "trust.pdf", b"%PDF trust", title="Trust instrument")
    deed = add(session, "deed.pdf", b"%PDF deed", title="Grant deed")

    out = email_thread.send_documents(
        session, "Estate packet", "Both files attached.", [trust.id, deed.id]
    )
    assert out["sent"] is True
    assert sent["to"] == sorted([FORD, GAURAV])
    assert {a["filename"] for a in sent["attachments"]} == {"trust.pdf", "deed.pdf"}
    assert out["contains_ssn"] == []
    # what went out is recorded in the shared thread, attachments named
    row = session.query(ChatMessage).one()
    assert "trust.pdf" in row.content and "deed.pdf" in row.content


def test_it_is_told_to_warn_before_forwarding_an_ssn(session, monkeypatch):
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: "ok")
    doc = add(session, "1040.txt", b"SSN 123-45-6789", title="2025 return", category="tax")
    out = email_thread.send_documents(session, "Tax", "attached", [doc.id])
    assert out["contains_ssn"] == ["2025 return"]
    assert "secure portal" in out["warn_before_forwarding"]


def test_nothing_attachable_does_not_send_a_hollow_email(session, monkeypatch):
    monkeypatch.setattr(
        email_thread.email_harvest, "send_message",
        lambda **kw: pytest.fail("must not send an email with no attachments"),
    )
    out = email_thread.send_documents(session, "Packet", "attached", ["doc_nope"])
    assert out["sent"] is False
    assert out["problems"][0]["problem"] == "no such document"


def test_the_tool_reaches_it(session, monkeypatch):
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: "ok")
    doc = add(session, "trust.pdf", b"%PDF trust", title="Trust")
    out = json.loads(execute_tool(session, "email_household_documents", {
        "subject": "Estate packet", "body": "Attached.", "document_ids": [doc.id],
    }))
    assert out["sent"] is True
    assert out["attached"][0]["filename"] == "trust.pdf"


def test_the_tool_refuses_an_empty_list(session):
    out = json.loads(execute_tool(session, "email_household_documents", {
        "subject": "s", "body": "b", "document_ids": [],
    }))
    assert "error" in out
