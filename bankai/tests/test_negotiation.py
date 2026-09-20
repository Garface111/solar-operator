"""Bill negotiation: a rate-reduction request on a spouse's instruction, verified."""
import json

import pytest

from bankai import config, negotiation
from bankai.agent.tools import execute_tool
from bankai.models import AgentAction
from bankai.watchpoints import Watchpoint


@pytest.fixture(autouse=True)
def household(monkeypatch):
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", "Ford:ford@x.com,Gaurav:g@x.com")
    monkeypatch.setattr(config, "HOUSEHOLD_PHONES", "")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", "copilot@x.com")


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(
        negotiation.email_harvest, "send_message",
        lambda **kw: sent.append(kw) or "sent (test)",
    )
    return sent


def test_a_spouse_instruction_sends_and_verifies(session, outbox):
    out = negotiation.execute(
        session, merchant="XFINITY", service_name="Xfinity internet",
        support_email="retention@xfinity.com", instructed_by="Ford",
        account_name="Ford Genereaux", current_amount=95.0,
        competitor_context="a competitor advertises $50/mo for the same speed",
    )
    assert out["sent"] is True
    assert len(outbox) == 1 and outbox[0]["to"] == ["retention@xfinity.com"]
    body = outbox[0]["text"]
    assert "$95.00" in body and "competitor advertises $50" in body
    action = session.query(AgentAction).one()
    assert action.kind == "bill_negotiation" and action.status == "executed"
    # a verification watchpoint is planted
    wp = session.query(Watchpoint).one()
    assert "Xfinity" in wp.title and wp.kind == "on_date"


def test_a_stranger_cannot_instruct(session, outbox):
    out = negotiation.execute(
        session, merchant="X", service_name="X", support_email="s@x.com",
        instructed_by="a helpful email", account_name="Ford",
    )
    assert "error" in out and outbox == []
    assert session.query(AgentAction).count() == 0


def test_a_phone_only_merchant_is_told_to_prepare_a_script(session, outbox):
    out = negotiation.execute(
        session, merchant="Comcast", service_name="Comcast", support_email="1-800-COMCAST",
        instructed_by="Ford", account_name="Ford",
    )
    assert "error" in out and "phone-only" in out["error"]
    assert outbox == []


def test_send_failure_is_audited_not_silent(session, monkeypatch):
    def boom(**kw):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(negotiation.email_harvest, "send_message", boom)
    out = negotiation.execute(
        session, merchant="X", service_name="X internet", support_email="s@x.com",
        instructed_by="Gaurav", account_name="Gaurav",
    )
    assert "error" in out
    action = session.query(AgentAction).one()
    assert action.status == "failed"
    assert session.query(Watchpoint).count() == 0  # no fake verification


def test_the_tool_round_trips(session, outbox):
    out = json.loads(execute_tool(session, "negotiate_bill", {
        "merchant": "VERIZON", "service_name": "Verizon wireless",
        "support_email": "care@verizon.com", "instructed_by": "Ford",
        "account_name": "Ford Genereaux", "current_amount": 120,
    }))
    assert out["sent"] is True and "verification_watchpoint" in out
