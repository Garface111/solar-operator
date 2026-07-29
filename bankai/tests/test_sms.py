import base64
import hashlib
import hmac

import pytest

from bankai import config
from bankai.messaging import sms, thread
from bankai.models import ChatMessage


@pytest.fixture(autouse=True)
def twilio_config(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(config, "TWILIO_FROM_NUMBER", "+18025550000")
    monkeypatch.setattr(
        config, "HOUSEHOLD_PHONES", "Ford:+1 (802) 555-1234, Sam:802-555-5678"
    )


def test_normalize_phone_variants():
    assert sms.normalize_phone("+1 (802) 555-1234") == "+18025551234"
    assert sms.normalize_phone("802-555-1234") == "+18025551234"
    assert sms.normalize_phone("18025551234") == "+18025551234"


def test_household_parsing_and_sender_identification():
    phones = sms.household_phones()
    assert phones == {"Ford": "+18025551234", "Sam": "+18025555678"}
    assert sms.identify_sender("+18025555678") == "Sam"
    assert sms.identify_sender("(802) 555-1234") == "Ford"
    assert sms.identify_sender("+19995550000") is None
    assert sms.configured()


def test_signature_validation_round_trip():
    url = "https://bankai.example.com/api/sms/webhook"
    params = {"From": "+18025551234", "Body": "hi", "To": "+18025550000"}
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    good = base64.b64encode(
        hmac.new(b"secret-token", payload.encode(), hashlib.sha1).digest()
    ).decode()
    assert sms.validate_signature(url, params, good)
    assert not sms.validate_signature(url, params, "bogus")
    assert not sms.validate_signature(url, {**params, "Body": "tampered"}, good)


def test_handle_inbound_relays_and_broadcasts(session, monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(sms, "send_sms", lambda to, body: sent.append((to, body)) or True)
    monkeypatch.setattr(
        thread.agent_chat, "run_turn",
        lambda s, messages, channel="web": "Your net worth is $10.",
    )

    reply = thread.handle_inbound(session, "Ford", "what's our net worth?")
    assert reply == "Your net worth is $10."
    # Relay of Ford's message goes only to Sam; reply goes to both.
    relay = [x for x in sent if x[1].startswith("Ford:")]
    assert relay == [("+18025555678", "Ford: what's our net worth?")]
    replies = [x for x in sent if x[1] == reply]
    assert {to for to, _ in replies} == {"+18025551234", "+18025555678"}
    # Thread persisted.
    rows = session.query(ChatMessage).all()
    assert [(m.role, m.speaker) for m in rows] == [("user", "Ford"), ("assistant", "copilot")]


def test_build_history_prefixes_speakers_and_starts_with_user(session):
    session.add(ChatMessage(channel="sms", role="assistant", speaker="copilot", content="hi"))
    session.add(ChatMessage(channel="sms", role="user", speaker="Sam", content="how much in savings?"))
    session.add(ChatMessage(channel="sms", role="assistant", speaker="copilot", content="$5,000."))
    session.flush()
    history = thread.build_history(session)
    assert history[0] == {"role": "user", "content": "[Sam] how much in savings?"}
    assert history[1]["role"] == "assistant"


def test_empty_inbound_ignored(session, monkeypatch):
    monkeypatch.setattr(sms, "send_sms", lambda to, body: True)
    assert thread.handle_inbound(session, "Ford", "   ") == ""
    assert session.query(ChatMessage).count() == 0
