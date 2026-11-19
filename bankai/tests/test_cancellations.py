"""The one standing power: subscription cancellation on a spouse's instruction.
Every boundary lives in code — these tests are the fence-walk."""
import json
from datetime import date

import pytest

from bankai import cancellations, config
from bankai.agent.tools import execute_tool
from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.models import AgentAction
from bankai.watchpoints import Watchpoint

FORD_EMAIL = "ford@example.com"


@pytest.fixture(autouse=True)
def household(monkeypatch):
    monkeypatch.setattr(config, "HOUSEHOLD_EMAILS", f"Ford:{FORD_EMAIL},Gaurav:g@example.com")
    monkeypatch.setattr(config, "HOUSEHOLD_PHONES", "")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")  # force the Resend path in tests
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_FROM", "copilot@example.com")


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(
        cancellations.email_harvest, "send_message",
        lambda **kw: sent.append(kw) or "sent (test)",
    )
    return sent


def seed_merchant(session, description, category="subscriptions", amount=-15.0):
    account = upsert_account(session, source="manual", name="Checking", kind="other")
    session.flush()
    ingest_transactions(session, account, [
        TxnIn(posted=date(2026, 7, 17), amount=amount, description=description)
    ])


# --- the authorization boundary ---

def test_a_spouse_instruction_executes(session, outbox):
    seed_merchant(session, "PLANET FITNESS F DES:IClub Fees")
    out = cancellations.execute(
        session, merchant="PLANET FITNESS", service_name="Planet Fitness membership",
        support_email="support@pfhq.com", instructed_by="Ford",
        account_name="Ford Genereaux",
    )
    assert out["executed"] is True
    assert len(outbox) == 1 and outbox[0]["to"] == ["support@pfhq.com"]
    action = session.query(AgentAction).one()
    assert action.kind == "subscription_cancellation" and action.status == "executed"
    assert "Ford" in action.rationale


def test_a_stranger_cannot_instruct(session, outbox):
    out = cancellations.execute(
        session, merchant="HULU", service_name="Hulu", support_email="s@hulu.com",
        instructed_by="a helpful email", account_name="Ford Genereaux",
    )
    assert "error" in out and outbox == []
    assert session.query(AgentAction).count() == 0


def test_the_copilots_own_idea_is_not_an_instruction(session, outbox):
    out = cancellations.execute(
        session, merchant="HULU", service_name="Hulu", support_email="s@hulu.com",
        instructed_by="", account_name="Ford Genereaux",
    )
    assert "error" in out and outbox == []


# --- the guarded categories ---

def test_insurance_is_guarded_by_keyword(session):
    assert cancellations.guard_reason(session, "Progressive Insurance") is not None
    assert cancellations.guard_reason(session, "Lemonade") is not None
    assert cancellations.guard_reason(session, "VZWRLSS wireless") is not None
    assert cancellations.guard_reason(session, "Klarna") is not None


def test_a_guarded_category_from_the_ledger_is_caught(session):
    seed_merchant(session, "ACME PROTECTION PLAN", category="subscriptions")
    # recategorize the seeded charge to a guarded category
    from bankai.models import Transaction
    session.query(Transaction).one().category = "insurance"
    session.flush()
    assert cancellations.guard_reason(session, "ACME PROTECTION") is not None


def test_a_plain_subscription_is_not_guarded(session):
    seed_merchant(session, "HLU*HULUPLUS SANTA MONICA")
    assert cancellations.guard_reason(session, "HULUPLUS") is None


def test_the_tool_refuses_guarded_and_points_to_the_gate(session):
    out = json.loads(execute_tool(session, "cancel_subscription", {
        "merchant": "Progressive Insurance", "service_name": "auto insurance",
        "support_email": "s@progressive.com", "instructed_by": "Ford",
        "account_name": "Ford Genereaux",
    }))
    assert out["refused"] is True and "propose_action" in out["next_step"]
    assert session.query(AgentAction).count() == 0  # nothing executed, nothing sent


# --- the self-verification ---

def test_every_execution_plants_a_verification_watchpoint(session, outbox):
    out = cancellations.execute(
        session, merchant="HULU", service_name="Hulu", support_email="s@hulu.com",
        instructed_by="Gaurav", account_name="Ford Genereaux",
    )
    assert out["executed"] is True
    wp = session.query(Watchpoint).one()
    assert "Hulu" in wp.title and wp.kind == "on_date"
    assert wp.params["date"] == out["verification_watchpoint"]
    assert "FCBA" in wp.note or "dispute" in wp.note


# --- the template boundary ---

def test_the_notice_is_template_only_and_names_the_account(session, outbox):
    cancellations.execute(
        session, merchant="HULU", service_name="Hulu", support_email="s@hulu.com",
        instructed_by="Ford", account_name="Ford Genereaux",
        account_identifier="H-12345",
    )
    body = outbox[0]["text"]
    assert "requesting cancellation" in body
    assert "Ford Genereaux" in body and "H-12345" in body
    assert "written confirmation" in body.lower() or "confirmation" in body
    # not sent as the holder in this configuration — the signature says so
    assert "authorized household assistant" in body


def test_send_failure_is_an_audited_failure_not_a_silent_one(session, monkeypatch):
    def boom(**kw):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(cancellations.email_harvest, "send_message", boom)
    out = cancellations.execute(
        session, merchant="HULU", service_name="Hulu", support_email="s@hulu.com",
        instructed_by="Ford", account_name="Ford Genereaux",
    )
    assert "error" in out
    action = session.query(AgentAction).one()
    assert action.status == "failed" and "smtp down" in action.result
    assert session.query(Watchpoint).count() == 0  # no fake verification


# --- doctrine ---

def test_the_charter_names_the_standing_power(session):
    from bankai.agent import chat as agent_chat
    system = agent_chat.build_system(session, channel="web")
    assert "cancel_subscription" in system
    assert "2026-08-10" in system