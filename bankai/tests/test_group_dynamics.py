"""Knowing when NOT to speak.

The copilot sits in a thread between two spouses. A participant that answers
every message is not a participant — it is a nuisance, and it makes the channel
unusable for the couple who own it. So it has to read who a message is FOR.

These tests pin the machinery: a decision to stay quiet is honoured, the message
is still HEARD (context accumulates either way), nothing is emailed, and the
audit trail distinguishes "heard, stayed out" from "stranger, ignored".
"""
import pytest

from bankai import config
from bankai.agent import chat as agent_chat
from bankai.connectors import resend_inbound
from bankai.messaging import email_thread
from bankai.models import ChatMessage

FORD = "ford@example.test"
GAURAV = "gaurav@example.test"


@pytest.fixture(autouse=True)
def household(monkeypatch):
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", f"Ford:{FORD},Gaurav:{GAURAV}")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", "copilot@example.test")


def inbound(body, sender=FORD):
    return {"from": sender, "subject": "Re: household", "message_id": "<m@x>",
            "references": "", "body": body}


# --- the primitive ---

def test_silence_is_recognised():
    assert agent_chat.is_silence(agent_chat.SILENCE)
    assert agent_chat.is_silence(f"  {agent_chat.SILENCE}  ")
    assert not agent_chat.is_silence("Your balance is $20,109.")
    assert not agent_chat.is_silence("")


def test_group_rules_are_given_on_shared_channels_only(session):
    """The dashboard is a private line — someone typing there is unambiguously
    talking to it, and going quiet would just look broken."""
    for channel in ("email", "sms"):
        assert "Stay silent" in agent_chat.build_system(session, channel)
    assert "Stay silent" not in agent_chat.build_system(session, "web")


def test_the_rules_say_both_halves(session):
    system = agent_chat.build_system(session, "email")
    assert "one spouse is addressing the other" in system
    assert "you are addressed by name" in system
    assert "asks you to weigh in" in system
    # and the exception that keeps silence from becoming negligence
    assert "let a real error stand" in system


# --- staying out ---

def test_a_message_between_the_spouses_gets_no_reply(session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        email_thread.email_harvest, "send_message", lambda **kw: sent.append(kw) or "ok"
    )
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": agent_chat.SILENCE,
    )
    reply = email_thread.process_message(
        session, inbound("Gaurav — can you grab the mail on your way in?")
    )

    assert agent_chat.is_silence(reply)
    assert sent == []  # nothing was emailed to anyone


def test_it_still_hears_what_it_did_not_answer(session, monkeypatch):
    """Silence is not deafness. The message must land in the shared thread so the
    context is there next time — but no reply may be recorded as spoken."""
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: "ok")
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": agent_chat.SILENCE,
    )
    email_thread.process_message(session, inbound("love you, see you at 6"))

    rows = session.query(ChatMessage).all()
    assert [(r.role, r.speaker) for r in rows] == [("user", "Ford")]
    assert "love you" in rows[0].content
    assert not any(r.role == "assistant" for r in rows)


# --- chiming in ---

def test_a_question_aimed_at_it_gets_answered(session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        email_thread.email_harvest, "send_message", lambda **kw: sent.append(kw) or "ok"
    )
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": "You have $20,109 in checking.",
    )
    reply = email_thread.process_message(session, inbound("what's our balance?"))

    assert reply == "You have $20,109 in checking."
    assert len(sent) == 1
    assert sent[0]["to"] == sorted([FORD, GAURAV])  # still a three-way answer
    assert any(r.role == "assistant" for r in session.query(ChatMessage).all())


def test_being_asked_to_weigh_in_mid_thread_is_answered(session, monkeypatch):
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: "ok")
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": "Since you asked: the mortgage clears on the 1st.",
    )
    reply = email_thread.process_message(
        session, inbound("Gaurav I think we're fine — copilot, back me up?")
    )
    assert "mortgage clears" in reply


# --- the audit trail keeps the three cases apart ---

def test_poll_records_silence_separately_from_ignoring_a_stranger(session, monkeypatch):
    resend_inbound.mark(session, "seed", "adopted")
    listing = [
        {"id": "quiet", "from": FORD, "to": ["copilot@example.test"], "cc": [],
         "created_at": "2026-07-29T10:00:00Z"},
        {"id": "stranger", "from": "spam@nowhere.test", "to": ["copilot@example.test"],
         "cc": [], "created_at": "2026-07-29T10:01:00Z"},
    ]
    full = {
        "quiet": {"id": "quiet", "from": FORD, "to": ["copilot@example.test"],
                  "subject": "s", "text": "Gaurav, dinner at 7?", "message_id": "<a@b>",
                  "attachments": []},
        "stranger": {"id": "stranger", "from": "spam@nowhere.test",
                     "to": ["copilot@example.test"], "subject": "s", "text": "buy now",
                     "message_id": "<c@d>", "attachments": []},
    }
    monkeypatch.setattr(resend_inbound, "list_inbound", lambda: listing)
    monkeypatch.setattr(resend_inbound, "fetch_inbound", lambda i: full[i])
    monkeypatch.setattr(email_thread.email_harvest, "send_message", lambda **kw: "ok")
    monkeypatch.setattr(
        email_thread.agent_chat, "run_turn",
        lambda s, history, channel="email": agent_chat.SILENCE,
    )

    out = email_thread.poll_resend(session)
    assert out["silent"] == 1 and out["ignored"] == 1 and out["answered"] == 0

    rows = {r.resend_id: r.outcome
            for r in session.query(resend_inbound.InboundEmail).all()}
    assert rows["quiet"] == "silent"      # heard, chose not to speak
    assert rows["stranger"] == "ignored"  # never read at all
