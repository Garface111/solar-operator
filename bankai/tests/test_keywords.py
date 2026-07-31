import pytest

from bankai import config
from bankai.messaging import sms, thread
from bankai.models import ChatMessage


@pytest.fixture(autouse=True)
def twilio_config(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(config, "TWILIO_FROM_NUMBER", "+18025550000")
    monkeypatch.setattr(config, "HOUSEHOLD_PHONES", "Ford:+18025551234,Partner:+18025555678")


@pytest.fixture()
def sent(monkeypatch):
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(sms, "send_sms", lambda to, body: captured.append((to, body)) or True)
    monkeypatch.setattr(
        thread.agent_chat, "run_turn", lambda s, m, channel="web": "Answer."
    )
    return captured


def test_stop_gets_no_reply_and_nothing_stored(session, sent):
    assert thread.handle_inbound(session, "Ford", "STOP") == ""
    assert sent == []
    assert session.query(ChatMessage).count() == 0


def test_help_replies_only_to_sender(session, sent):
    reply = thread.handle_inbound(session, "Ford", "help")
    assert reply == thread.HELP_TEXT
    assert sent == [("+18025551234", thread.HELP_TEXT)]
    assert session.query(ChatMessage).count() == 0


def test_first_contact_sends_welcome_then_answers(session, sent):
    thread.handle_inbound(session, "Ford", "what's our balance?")
    welcome = [b for _, b in sent if b == thread.WELCOME_TEXT]
    assert welcome == [thread.WELCOME_TEXT]  # sent once, to the new member
    assert sent[0] == ("+18025551234", thread.WELCOME_TEXT)
    # Second message: no welcome again.
    sent.clear()
    thread.handle_inbound(session, "Ford", "and savings?")
    assert all(b != thread.WELCOME_TEXT for _, b in sent)


def test_start_keyword_gets_welcome_not_agent(session, sent):
    reply = thread.handle_inbound(session, "Partner", "START")
    assert reply == thread.WELCOME_TEXT
    assert session.query(ChatMessage).count() == 0  # keyword not stored as conversation
