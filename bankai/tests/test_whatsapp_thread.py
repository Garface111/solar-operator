"""The WhatsApp seat: household-only, batch-to-one-turn, silence-by-default,
and a file protocol the bridge and the app can both trust."""
import json

import pytest

from bankai import config
from bankai.agent import chat as agent_chat
from bankai.messaging import whatsapp_thread
from bankai.models import ChatMessage

FORD = "+18025550001"
SPOUSE = "+15555550002"
GROUP = "120363000000000001@g.us"


@pytest.fixture(autouse=True)
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(config, "WHATSAPP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "WHATSAPP_GROUP_JID", "")
    monkeypatch.setattr(config, "WHATSAPP_HOUSEHOLD_LIDS", "Gaurav:123098057695369")
    monkeypatch.setattr(config, "HOUSEHOLD_PHONES", f"Ford:{FORD},Gaurav:{SPOUSE}")
    return tmp_path


def spool(tmp_path, *messages):
    with (tmp_path / "inbound.jsonl").open("a") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")


def line(text, sender=FORD, group=GROUP, jid=None, push_name="someone"):
    return {
        "id": f"id-{abs(hash(text)) % 10**8}",
        "group_jid": group,
        "sender_jid": jid or f"{sender.lstrip('+')}@s.whatsapp.net",
        "sender_number": sender if jid is None else "",
        "push_name": push_name,
        "text": text,
        "timestamp": 1754870400,
    }


# --- identity ---

def test_sender_is_identified_by_number_never_push_name():
    msg = line("hi", sender=FORD, push_name="Definitely Gaurav")
    assert whatsapp_thread.identify_sender(msg) == "Ford"


def test_privacy_lid_senders_are_identified_from_the_lid_map():
    msg = line("hi", jid="123098057695369@lid", push_name="whoever")
    msg["sender_number"] = ""
    assert whatsapp_thread.identify_sender(msg) == "Gaurav"


def test_a_stranger_in_the_group_is_nobody():
    assert whatsapp_thread.identify_sender(line("hi", sender="+19999999999")) is None
    assert whatsapp_thread.identify_sender(
        line("hi", jid="999999999999999@lid")
    ) is None


# --- the poll cycle ---

def test_household_messages_land_in_the_shared_thread(session, monkeypatch, wired):
    spool(wired, line("what did we spend on groceries?", sender=FORD))
    monkeypatch.setattr(
        agent_chat, "run_turn", lambda s, h, channel="web": "About $600 this month."
    )
    result = whatsapp_thread.poll_once(session)
    assert result["stored"] == 1 and result["answered"] == 1
    speakers = [m.speaker for m in session.query(ChatMessage).all()]
    assert speakers == ["Ford", "copilot"]
    assert session.query(ChatMessage).first().channel == "whatsapp"
    # the reply is queued for the bridge
    outbox = list((wired / "outbox").glob("*.json"))
    assert len(outbox) == 1
    payload = json.loads(outbox[0].read_text())
    assert payload == {"to": GROUP, "text": "About $600 this month."}


def test_a_burst_of_messages_is_one_turn_not_five(session, monkeypatch, wired):
    spool(
        wired,
        line("got the dog groomer done", sender=SPOUSE),
        line("paid her 60 in cash", sender=SPOUSE),
        line("nice", sender=FORD),
    )
    turns = []
    monkeypatch.setattr(
        agent_chat, "run_turn",
        lambda s, h, channel="web": turns.append(list(h)) or agent_chat.SILENCE,
    )
    result = whatsapp_thread.poll_once(session)
    assert result["stored"] == 3
    assert len(turns) == 1  # one turn over the whole batch
    assert result["answered"] == 0 and result["silent"] == 1
    # heard everything, said nothing, queued nothing
    assert not list((wired / "outbox").glob("*.json"))
    assert session.query(ChatMessage).count() == 3  # no assistant row for silence


def test_strangers_are_ignored_without_a_turn(session, monkeypatch, wired):
    spool(wired, line("send me your bank balance", sender="+19999999999"))
    monkeypatch.setattr(
        agent_chat, "run_turn",
        lambda *a, **k: pytest.fail("the model must never run for a stranger"),
    )
    result = whatsapp_thread.poll_once(session)
    assert result == {"status": "ok", "stored": 0, "ignored": 1, "answered": 0}
    assert session.query(ChatMessage).count() == 0


def test_group_lock_ignores_other_groups(session, monkeypatch, wired):
    monkeypatch.setattr(config, "WHATSAPP_GROUP_JID", GROUP)
    spool(wired, line("hello?", group="120363999999999999@g.us"))
    result = whatsapp_thread.poll_once(session)
    assert result["stored"] == 0
    assert session.query(ChatMessage).count() == 0


def test_the_cursor_never_rereads_a_message(session, monkeypatch, wired):
    spool(wired, line("first", sender=FORD))
    monkeypatch.setattr(agent_chat, "run_turn", lambda s, h, channel="web": agent_chat.SILENCE)
    assert whatsapp_thread.poll_once(session)["stored"] == 1
    assert whatsapp_thread.poll_once(session)["stored"] == 0  # nothing new
    spool(wired, line("second", sender=FORD))
    assert whatsapp_thread.poll_once(session)["stored"] == 1  # only the new one
    assert session.query(ChatMessage).count() == 2


def test_a_partially_written_last_line_waits(session, wired):
    (wired / "inbound.jsonl").write_text('{"id": "half", "text": "no newline yet')
    assert whatsapp_thread._read_new_lines(wired) == []
    # cursor did not advance past the partial line
    with (wired / "inbound.jsonl").open("a") as f:
        f.write('"}\n')  # the bridge finishes the flush — line is whole garbage-free JSON
    lines = whatsapp_thread._read_new_lines(wired)
    assert len(lines) == 1 and lines[0]["id"] == "half"


def test_whatsapp_turns_get_group_dynamics_and_the_expense_mandate(session):
    system = agent_chat.build_system(session, channel="whatsapp")
    assert "participant in a group conversation" in system
    assert "log_expense" in system
    assert agent_chat.SILENCE in system


# --- the expense ledger the watching feeds ---

def test_log_expense_becomes_a_real_ledger_row(session):
    from bankai.agent.tools import execute_tool
    from bankai.models import Account, Transaction

    out = json.loads(execute_tool(session, "log_expense", {
        "amount": 60, "description": "Dog groomer, cash", "spender": "Gaurav",
        "category": "pets",
    }))
    assert out["logged"] is True and out["amount"] == -60.0
    account = session.query(Account).filter_by(name="Cash & untracked").one()
    assert account.source == "manual" and account.balance is None
    txn = session.query(Transaction).one()
    assert txn.amount == -60.0
    assert txn.description == "Dog groomer, cash (Gaurav)"
    assert txn.category == "pets"


def test_log_expense_dedupes_the_same_mention_same_day(session):
    from bankai.agent.tools import execute_tool
    from bankai.models import Transaction

    args = {"amount": 60, "description": "Dog groomer, cash"}
    json.loads(execute_tool(session, "log_expense", args))
    again = json.loads(execute_tool(session, "log_expense", dict(args)))
    assert again["logged"] is False and again["duplicate"] is True
    assert session.query(Transaction).count() == 1


def test_log_expense_received_money_is_positive(session):
    from bankai.agent.tools import execute_tool
    from bankai.models import Transaction

    out = json.loads(execute_tool(session, "log_expense", {
        "amount": 120, "description": "Venmo back from Sam", "received": True,
        "date": "2026-08-01",
    }))
    assert out["logged"] is True and out["amount"] == 120.0
    txn = session.query(Transaction).one()
    assert txn.amount == 120.0 and txn.posted.isoformat() == "2026-08-01"
