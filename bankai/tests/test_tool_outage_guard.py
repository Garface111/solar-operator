"""A reply that says the copilot's own tools are missing is an artifact, not an
answer. Pinned from a live failure: one such reply landed in the thread, and
within twenty minutes two healthy turns had copied it without calling a tool —
one of them in a check-in email to the household."""
import pytest

from bankai import config
from bankai.agent import chat as agent_chat
from bankai.agent import verify
from bankai.agent.backends import claude_cli, grok_backend
from bankai.messaging import thread
from bankai.models import ChatMessage

# The three shapes it actually took, with the household's details removed.
SEEN_LIVE = [
    "I cannot access the household's financial data in this session. The deferred "
    "MCP tools for retrieving accounts, recurring bills, goals, and loans are not "
    "callable.",
    "I cannot reach the accounts in this session — the MCP connection is not "
    "available to me right now. But your persistent flags show overdue items.",
    "I cannot complete this monthly review. The MCP tools needed to retrieve "
    "current accounts, spending anomalies and cash flow are not callable.",
]

# True, useful sentences about real limits. None of them may be caught.
LEGITIMATE = [
    "The Riverside Credit Union connection has been unavailable since August 30, "
    "so the checking balance is 74 days old.",
    "I can't see charges on the store card after the last export — it has no feed.",
    "I have no tool that moves money; you would need to make the transfer yourself.",
    "I couldn't find a transaction matching $48.20 at Globex in October.",
    "Net worth is $260,000.00: $60,000.00 liquid and $200,000.00 invested.",
    "That balance is not available until the statement posts on the 15th.",
]


@pytest.fixture(autouse=True)
def _fixed_chain(monkeypatch):
    monkeypatch.setattr(config, "ROUTER_ENABLED", False)
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli")
    monkeypatch.setattr(config, "CLAUDE_CLI_MODEL", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_EFFORT", "")
    monkeypatch.setattr(verify, "VERIFY_REPLIES", False)


@pytest.mark.parametrize("text", SEEN_LIVE)
def test_the_live_shapes_are_recognised(text):
    assert verify.looks_like_tool_outage(text)
    assert verify.is_error_template(text)


@pytest.mark.parametrize("text", LEGITIMATE)
def test_real_limits_are_not_mistaken_for_an_outage(text):
    assert not verify.looks_like_tool_outage(text)
    assert not verify.is_error_template(text)


def test_a_draft_that_blames_its_tools_gets_one_correction(session, monkeypatch):
    asked = []

    def cli(s, system, messages, **k):
        asked.append(messages)
        return SEEN_LIVE[0] if len(asked) == 1 else "Checking holds $1,250.00."

    monkeypatch.setattr(claude_cli, "run", cli)
    reply = agent_chat.run_turn(session, [{"role": "user", "content": "balance?"}])

    assert reply == "Checking holds $1,250.00."
    assert len(asked) == 2
    correction = asked[1][-1]
    assert correction["role"] == "user" and "available right now" in correction["content"]
    assert asked[1][0] == {"role": "user", "content": "balance?"}  # the question survives


def test_a_backend_that_insists_fails_the_turn_instead_of_shipping_it(session, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli,grok")
    calls = {"cli": 0}

    def cli(s, system, messages, **k):
        calls["cli"] += 1
        return SEEN_LIVE[1]

    monkeypatch.setattr(claude_cli, "run", cli)
    monkeypatch.setattr(grok_backend, "run", lambda s, system, messages, **k: "Checking holds $1,250.00.")
    assert agent_chat.run_turn(session, [{"role": "user", "content": "balance?"}]) == "Checking holds $1,250.00."
    assert calls["cli"] == 2  # the draft, one correction, then the next backend

    # with nothing behind it, the turn raises: no reply is better than that reply
    monkeypatch.setattr(config, "LLM_BACKEND", "claude-cli")
    with pytest.raises(RuntimeError, match="tools were unavailable"):
        agent_chat.run_turn(session, [{"role": "user", "content": "balance?"}])


def test_stored_error_replies_are_withheld_from_history_not_deleted(session):
    rows = [
        ("user", "Avery", "what do we owe on the card?"),
        ("assistant", "copilot", SEEN_LIVE[0]),
        ("user", "Avery", "try again"),
        ("assistant", "copilot", "The card owes $412.07."),
        ("user", "Avery", "and checking?"),
        ("assistant", "copilot", "I need to stop myself here — I cannot find it in your data."),
    ]
    for role, speaker, content in rows:
        session.add(ChatMessage(channel="web", role=role, speaker=speaker, content=content))
        session.flush()

    history = thread.build_history(session)

    said = [m["content"] for m in history if m["role"] == "assistant"]
    assert said == ["The card owes $412.07."]
    # every question is still there, in order, and the thread itself is intact
    assert [m["content"] for m in history if m["role"] == "user"] == [
        "[Avery] what do we owe on the card?", "[Avery] try again", "[Avery] and checking?",
    ]
    assert session.query(ChatMessage).count() == len(rows)
