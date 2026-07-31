"""Inbound email over Resend: only our mail, only household senders, exactly once."""
import pytest

from bankai import config
from bankai.connectors import resend_inbound
from bankai.messaging import email_thread
from bankai.models import ChatMessage, Document

FORD = "ford.genereaux@gmail.com"
SPOUSE = "gplusch@gmail.com"
OURS = "copilot@agent.arrayoperator.com"


@pytest.fixture(autouse=True)
def wired(monkeypatch, tmp_path):
    from bankai import vault
    monkeypatch.setattr(vault, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", OURS)
    monkeypatch.setattr(config, "EMAIL_INBOUND_ADDRESS", "")
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", f"Ford:{FORD},Partner:{SPOUSE}")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")


def summary(msg_id, sender=FORD, to=OURS, created="2026-07-29T10:00:00Z"):
    return {"id": msg_id, "from": sender, "to": [to], "cc": [], "created_at": created}


def full(msg_id, sender=FORD, subject="Question", text="how are we doing?", attachments=None):
    return {
        "id": msg_id, "from": sender, "to": [OURS], "subject": subject,
        "text": text, "message_id": f"<{msg_id}@mail>", "created_at": "2026-07-29T10:00:00Z",
        "attachments": attachments or [],
    }


def wire(monkeypatch, listing, details, sent=None, reply="Net worth is $1.3M."):
    monkeypatch.setattr(resend_inbound, "list_inbound", lambda: listing)
    monkeypatch.setattr(resend_inbound, "fetch_inbound", lambda i: details[i])
    monkeypatch.setattr(
        email_thread.email_harvest, "send_message",
        lambda **kw: (sent.append(kw) if sent is not None else None) or "ok",
    )
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn", lambda s, history, channel="email": reply
    )


# --- readiness ---

def test_resend_alone_is_enough_now(monkeypatch):
    """No mailbox, no app password — the key covers both directions."""
    assert resend_inbound.configured() is True
    assert email_thread.configured() is True


# --- addressing ---

def test_only_mail_addressed_to_this_copilot_counts():
    assert resend_inbound.addressed_to_us(summary("a")) is True
    # the same Resend account receives for the household's other agents
    assert resend_inbound.addressed_to_us(summary("b", to="repairs@agent.arrayoperator.com")) is False
    assert resend_inbound.addressed_to_us(summary("c", to="sovereign@agent.arrayoperator.com")) is False


def test_cc_counts_as_addressed_to_us():
    msg = {"id": "x", "to": ["someone@else.com"], "cc": [OURS]}
    assert resend_inbound.addressed_to_us(msg) is True


# --- the backlog guard ---

def test_first_poll_adopts_history_without_answering(session, monkeypatch):
    sent = []
    wire(monkeypatch, [summary("old1"), summary("old2")], {}, sent)
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda *a, **k: pytest.fail("must not answer pre-existing mail on first run"),
    )
    result = email_thread.poll_resend(session)
    assert result == {"status": "initialized", "adopted": 2}
    assert sent == []
    assert session.query(ChatMessage).count() == 0


# --- answering ---

def test_new_household_email_is_answered_once(session, monkeypatch):
    resend_inbound.mark(session, "seed", "adopted")  # past initialization
    sent = []
    wire(monkeypatch, [summary("m1")], {"m1": full("m1")}, sent)

    first = email_thread.poll_resend(session)
    assert first["answered"] == 1
    assert sent[0]["to"] == sorted([FORD, SPOUSE])  # three-way
    assert sent[0]["headers"]["In-Reply-To"] == "<m1@mail>"

    # polling again must not answer the same email twice
    second = email_thread.poll_resend(session)
    assert second["answered"] == 0
    assert len(sent) == 1


def test_other_agents_mail_is_never_read(session, monkeypatch):
    resend_inbound.mark(session, "seed", "adopted")
    wire(monkeypatch, [summary("repair1", to="repairs@agent.arrayoperator.com")], {})
    monkeypatch.setattr(
        resend_inbound, "fetch_inbound",
        lambda i: pytest.fail("must not even fetch another agent's mail"),
    )
    result = email_thread.poll_resend(session)
    assert result["answered"] == 0
    assert session.query(ChatMessage).count() == 0


def test_stranger_is_ignored_and_not_retried(session, monkeypatch):
    resend_inbound.mark(session, "seed", "adopted")
    sent = []
    wire(monkeypatch, [summary("s1", sender="phisher@evil.com")],
         {"s1": full("s1", sender="phisher@evil.com")}, sent)
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda *a, **k: pytest.fail("the model must never run for a stranger"),
    )
    result = email_thread.poll_resend(session)
    assert result["ignored"] == 1 and result["answered"] == 0
    assert sent == []
    assert session.query(ChatMessage).count() == 0
    # marked, so it is not reconsidered every minute forever
    assert "s1" in resend_inbound.seen_ids(session)


# --- attachments ---

def test_emailed_attachment_lands_in_the_vault(session, monkeypatch):
    import base64
    resend_inbound.mark(session, "seed", "adopted")
    attachment = {
        "filename": "Family_Trust_Agreement.pdf",
        "content": base64.b64encode(b"trust document bytes").decode(),
    }
    wire(monkeypatch, [summary("m2")],
         {"m2": full("m2", subject="Our trust", attachments=[attachment])}, [])
    result = email_thread.poll_resend(session)
    assert result["documents_filed"] == 1
    doc = session.query(Document).one()
    assert doc.title.startswith("Family_Trust_Agreement")
    assert doc.category == "estate"
    assert "Emailed in by Ford" in doc.summary


def test_a_broken_attachment_does_not_lose_the_message(session, monkeypatch):
    resend_inbound.mark(session, "seed", "adopted")
    wire(monkeypatch, [summary("m3")],
         {"m3": full("m3", attachments=[{"filename": "x.pdf", "content": "!!not base64!!"}])}, [])
    result = email_thread.poll_resend(session)
    assert result["answered"] == 1  # the conversation still happened
    assert session.query(Document).count() == 0


# --- body handling ---

def test_html_only_email_is_read_as_text():
    msg = {"html": "<div>Yes, <b>cancel</b> it.</div><p>Thanks</p>", "text": ""}
    text = resend_inbound.body_text(msg)
    assert "Yes, cancel it." in text
    assert "<" not in text


def test_quoted_history_is_stripped_from_html_replies(session, monkeypatch):
    resend_inbound.mark(session, "seed", "adopted")
    seen = {}
    body = (
        "Cancel it please.\n\nOn Tue, Jul 28, 2026 at 9:41 AM BankAI wrote:\n"
        "> Are you still using PlayStation Plus?"
    )
    wire(monkeypatch, [summary("m4")], {"m4": full("m4", text=body)}, [])
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": seen.setdefault("h", history) and "" or "ok",
    )
    email_thread.poll_resend(session)
    last = seen["h"][-1]["content"]
    assert last == "[Ford] Cancel it please."


# --- exactly-once under concurrency ---

def test_a_claimed_message_cannot_be_claimed_twice(session):
    assert resend_inbound.claim(session, "dup1") is True
    # a second worker racing for the same email loses
    assert resend_inbound.claim(session, "dup1") is False


def test_released_claim_can_be_retried(session):
    assert resend_inbound.claim(session, "retry1") is True
    resend_inbound.release(session, "retry1")
    assert resend_inbound.claim(session, "retry1") is True


def test_settle_records_the_outcome(session):
    resend_inbound.claim(session, "s1")
    resend_inbound.settle(session, "s1", "answered")
    row = session.query(resend_inbound.InboundEmail).filter_by(resend_id="s1").one()
    assert row.outcome == "answered"


def test_a_failed_turn_is_retried_not_swallowed(session, monkeypatch):
    """A model failure must not silently consume the email — it should be
    answerable on the next poll."""
    resend_inbound.mark(session, "seed", "adopted")
    calls = {"n": 0}

    def boom(s, history, channel="email"):
        calls["n"] += 1
        raise RuntimeError("backend down")

    wire(monkeypatch, [summary("m9")], {"m9": full("m9")}, [])
    monkeypatch.setattr(email_thread.agent_chat, "run_turn", boom)
    email_thread.poll_resend(session)
    assert calls["n"] == 1
    assert "m9" not in resend_inbound.seen_ids(session)  # released for retry

    # next poll: a working backend answers it
    sent = []
    wire(monkeypatch, [summary("m9")], {"m9": full("m9")}, sent)
    result = email_thread.poll_resend(session)
    assert result["answered"] == 1 and len(sent) == 1
