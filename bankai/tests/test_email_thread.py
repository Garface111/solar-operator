"""Email as a channel into the shared thread — and the guard on who may use it."""
import email.message

import pytest

from bankai import config
from bankai.connectors import email_harvest
from bankai.messaging import email_thread
from bankai.models import ChatMessage

FORD = "ford.genereaux@gmail.com"
SPOUSE = "sam@example.com"


@pytest.fixture(autouse=True)
def household(monkeypatch):
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", f"Ford:{FORD},Sam:{SPOUSE}")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "copilot@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "EMAIL_FROM", "")


def raw_email(sender, subject="Question", body="How are we doing?", message_id="<abc@mail>"):
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "copilot@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


# --- who is allowed to talk to it ---

def test_identifies_household_senders():
    assert email_thread.identify_sender(f"Ford G <{FORD}>") == "Ford"
    assert email_thread.identify_sender(SPOUSE.upper()) == "Sam"


def test_unknown_senders_are_not_identified():
    assert email_thread.identify_sender("stranger@phisher.com") is None
    assert email_thread.identify_sender("") is None


def test_stranger_email_is_ignored_entirely(session, monkeypatch):
    """The inbox is a public address and the copilot can read everything about
    this household — an unknown sender must get no reply and leave no trace."""
    sent = []
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: sent.append(kw) or "ok")
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda *a, **k: pytest.fail("the model must never run for a stranger"),
    )
    parsed = email_thread._parse(raw_email("stranger@phisher.com"))
    assert email_thread.process_message(session, parsed) is None
    assert sent == []
    assert session.query(ChatMessage).count() == 0


# --- body cleaning ---

def test_strips_quoted_reply_history():
    body = (
        "Yes, cancel it.\n\n"
        "On Tue, Jul 28, 2026 at 9:41 AM BankAI <copilot@example.com> wrote:\n"
        "> Are you still using PlayStation Plus?\n> It runs $9.99/mo.\n"
    )
    cleaned = email_thread.strip_quoted(body)
    assert cleaned == "Yes, cancel it."
    assert "PlayStation" not in cleaned


def test_strips_outlook_style_history():
    body = "Sounds good.\n\n-----Original Message-----\nFrom: BankAI\nold stuff"
    assert email_thread.strip_quoted(body) == "Sounds good."


def test_plain_body_prefers_text_over_html():
    msg = email.message.EmailMessage()
    msg.set_content("the plain version")
    msg.add_alternative("<p>the html version</p>", subtype="html")
    assert "plain version" in email_thread.plain_body(msg)


# --- the three-way reply ---

def test_reply_goes_to_the_whole_household():
    assert email_thread.household_recipients() == sorted([FORD, SPOUSE])


def test_reply_threads_under_the_original(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        email_thread.email_harvest, "send_message", lambda **kw: sent.update(kw) or "ok"
    )
    email_thread.send_reply(
        reply_text="Net worth is $1.3M.", subject="Question",
        message_id="<abc@mail>", references="", recipients=[FORD, SPOUSE],
    )
    assert sent["subject"] == "Re: Question"
    assert sent["headers"]["In-Reply-To"] == "<abc@mail>"
    assert sent["headers"]["References"] == "<abc@mail>"
    assert sent["to"] == [FORD, SPOUSE]


def test_references_chain_grows_so_long_threads_stay_together():
    headers = email_thread.threading_headers("<third@mail>", "<first@mail> <second@mail>")
    assert headers["References"] == "<first@mail> <second@mail> <third@mail>"


def test_first_message_without_an_id_sends_without_threading_headers():
    assert email_thread.threading_headers("", "") == {}


def test_reply_does_not_double_prefix_re(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        email_thread.email_harvest, "send_message", lambda **kw: sent.update(kw) or "ok"
    )
    email_thread.send_reply(
        reply_text="x", subject="Re: Question", message_id="", references="",
        recipients=[FORD],
    )
    assert sent["subject"] == "Re: Question"


def test_household_email_runs_a_turn_and_replies_to_both(session, monkeypatch):
    sent = []
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: sent.append(kw) or "ok")
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": f"[{channel}] you have $10.",
    )
    parsed = email_thread._parse(raw_email(f"Ford <{FORD}>", body="how are we doing?"))
    reply = email_thread.process_message(session, parsed)

    assert reply == "[email] you have $10."
    # stored in the ONE shared thread, speaker-labelled, on the email channel
    rows = session.query(ChatMessage).all()
    assert {(r.role, r.speaker, r.channel) for r in rows} == {
        ("user", "Ford", "email"), ("assistant", "copilot", "email"),
    }
    assert len(sent) == 1
    assert sent[0]["to"] == sorted([FORD, SPOUSE])


def test_spouse_email_joins_the_same_thread(session, monkeypatch):
    """Context is continuous: the husband's email sees Ford's earlier messages."""
    session.add(ChatMessage(channel="web", role="user", speaker="Ford", content="earlier"))
    session.commit()
    seen = {}
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: "ok")
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": seen.setdefault("history", history) and "" or "ok",
    )
    parsed = email_thread._parse(raw_email(f"Sam <{SPOUSE}>", body="and now?"))
    email_thread.process_message(session, parsed)
    contents = [m["content"] for m in seen["history"]]
    assert any("earlier" in c for c in contents)
    assert any("[Sam] and now?" == c for c in contents)


def test_empty_body_after_stripping_is_not_answered(session, monkeypatch):
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda *a, **k: pytest.fail("must not run a turn on an empty message"),
    )
    parsed = email_thread._parse(raw_email(f"Ford <{FORD}>", body="> only quoted text"))
    assert email_thread.process_message(session, parsed) is None


def test_not_configured_without_household_addresses(monkeypatch):
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", "")
    assert email_thread.configured() is False


def test_resend_key_alone_cannot_receive(monkeypatch):
    """Resend is send-only: without a mailbox to poll there is no inbound half,
    so the thread must not report itself ready."""
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    assert email_harvest.can_send() is True
    assert email_thread.configured() is False
