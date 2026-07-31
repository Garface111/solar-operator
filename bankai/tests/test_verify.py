"""Pure-logic tests for the adversarial conscience.

No network, no LLM, no database: backends are plain callables with the same
signature as `bankai.agent.backends.*.run` — (session, system, messages) -> str
— that return canned strings. The session argument is unused by verify.py, so
these pass None.
"""
import pytest

from bankai.agent import verify


# --- fake backends -------------------------------------------------------


def canned(*replies):
    """A backend that returns `replies` in order and records every call."""
    calls = []
    remaining = list(replies)

    def run(session, system, messages):
        calls.append({"session": session, "system": system, "messages": messages})
        return remaining.pop(0) if remaining else remaining_default

    remaining_default = replies[-1] if replies else ""
    run.calls = calls
    return run


def two_phase(critic_reply, revision_reply):
    """Backend that answers the critic prompt and the revision prompt differently."""
    calls = []

    def run(session, system, messages):
        calls.append(system)
        if system is verify.CRITIC_SYSTEM:
            return critic_reply
        return revision_reply

    run.calls = calls
    return run


def exploding(exc=RuntimeError("backend is down")):
    def run(session, system, messages):
        raise exc

    return run


# --- is_consequential ----------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "Your Ridgeline Credit Union balance is $1,240.50 as of yesterday.",
        "That leaves exactly $100 in the buffer account.",
        "The card's APR is 24.99%.",
        "Utilities are up 12 percent over last quarter.",
        "You can afford the new dishwasher without touching savings.",
        "I recommend consolidating the two streaming subscriptions.",
        "You should look at the homeowners policy before renewal.",
        "Want me to draft a note to cancel the gym membership?",
        "The easiest fix is to transfer the surplus into the mortgage escrow.",
        "The policy lapses on 2026-09-01 unless it is renewed.",
        "The filing deadline is October 15 for the extension.",
        "Payment is due 9/30 per the contract.",
    ],
)
def test_is_consequential_positives(reply):
    assert verify.is_consequential(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        "Got it — saved that to my notes.",
        "You have four linked accounts right now.",
        "That charge was $4.75 at the corner bakery.",
        "The deed and the vehicle title are both in the vault.",
        "Sure, I can pull that up.",
    ],
)
def test_is_consequential_negatives(reply):
    assert verify.is_consequential(reply) is False


def test_dollar_gate_uses_the_largest_amount_in_the_reply():
    # A cheap line item alongside a consequential total still trips the gate.
    assert verify.is_consequential("Coffee was $4.10; rent was $2,150.00.") is True
    assert verify.is_consequential("Coffee $4.10, parking $6.00, snack $2.25.") is False


# --- critique ------------------------------------------------------------


CLEAN_JSON = (
    '{"verdict": "revise", '
    '"problems": ["The $1,240 total does not match the three line items, which sum to $1,180."], '
    '"severity": "high"}'
)


def test_critique_parses_clean_json():
    backend = canned(CLEAN_JSON)
    result = verify.critique(None, [], "Your total is $1,240.", backend)

    assert result["verdict"] == "revise"
    assert result["severity"] == "high"
    assert len(result["problems"]) == 1
    assert "$1,240" in result["problems"][0]


def test_critique_parses_json_buried_in_prose_and_fences():
    dirty = (
        "I reviewed the draft carefully and found one arithmetic error.\n\n"
        "```json\n"
        '{"verdict": "revise", "problems": ["The 12% figure is not in the tool data."], '
        '"severity": "low"}\n'
        "```\n\n"
        "Let me know if you want more detail."
    )
    result = verify.critique(None, [], "Spending rose 12%.", canned(dirty))

    assert result["verdict"] == "revise"
    assert result["problems"] == ["The 12% figure is not in the tool data."]
    assert result["severity"] == "low"


def test_critique_ignores_a_leading_unparseable_brace_block():
    dirty = (
        "Here is my scratch work {this is not json at all} and here is the verdict:\n"
        '{"verdict": "pass", "problems": [], "severity": "low"}'
    )
    result = verify.critique(None, [], "Balance is $500.", canned(dirty))

    assert result["verdict"] == "pass"
    assert result["problems"] == []


@pytest.mark.parametrize(
    "garbage",
    [
        "Looks fine to me!",
        "",
        "   ",
        "{not even close to json",
        '{"verdict": "maybe", "problems": [], "severity": "low"}',
    ],
)
def test_critique_degrades_to_pass_on_garbage(garbage):
    result = verify.critique(None, [], "You should transfer $900.", canned(garbage))

    assert result["verdict"] == "pass"
    assert result["problems"] == []
    assert result["note"]  # says why the critic verdict was not used


def test_critique_treats_revise_with_no_problems_as_pass():
    result = verify.critique(
        None, [], "You should transfer $900.",
        canned('{"verdict": "revise", "problems": [], "severity": "high"}'),
    )

    assert result["verdict"] == "pass"
    assert "listed no problems" in result["note"]


def test_critique_normalizes_a_bare_string_problems_field():
    result = verify.critique(
        None, [], "You can afford it.",
        canned('{"verdict": "revise", "problems": "Balance data is 6 weeks stale.", '
               '"severity": "low"}'),
    )

    assert result["problems"] == ["Balance data is 6 weeks stale."]


def test_critique_never_raises_when_the_backend_throws():
    result = verify.critique(None, [], "You should cancel it.", exploding())

    assert result["verdict"] == "pass"
    assert result["problems"] == []
    assert "backend failed" in result["note"]


def test_critique_sends_the_critic_system_and_the_conversation():
    backend = canned('{"verdict": "pass", "problems": [], "severity": "low"}')
    messages = [
        {"role": "user", "content": "[Wren] how much did we spend on utilities?"},
        {"role": "assistant", "content": "get_transactions returned 3 utility charges."},
        {"role": "user", "content": "[Wren] and the total?"},
    ]
    verify.critique(None, messages, "Utilities came to $412.90 last month.", backend)

    call = backend.calls[0]
    assert call["system"] is verify.CRITIC_SYSTEM
    prompt = call["messages"][-1]["content"]
    assert "$412.90" in prompt  # the draft under review
    assert "get_transactions returned 3 utility charges." in prompt  # the tool data
    assert "how much did we spend on utilities?" in prompt


def test_critique_truncates_long_history_to_recent_turns():
    backend = canned('{"verdict": "pass", "problems": [], "severity": "low"}')
    messages = [{"role": "user", "content": f"turn number {i}"} for i in range(40)]
    verify.critique(None, messages, "Noted.", backend)

    prompt = backend.calls[0]["messages"][-1]["content"]
    assert "turn number 39" in prompt
    assert "turn number 0" not in prompt


# --- verified_turn -------------------------------------------------------


def test_verified_turn_passes_through_on_pass():
    reply = "You should be fine on the escrow — the balance is $2,300."
    backend = canned('{"verdict": "pass", "problems": [], "severity": "low"}')

    final, report = verify.verified_turn(None, [], reply, backend)

    assert final == reply
    assert report["verified"] is True
    assert report["verdict"] == "pass"
    assert report["problems"] == []
    assert report["revised"] is False
    assert len(backend.calls) == 1  # critic only, no revision call


def test_verified_turn_revises_once_on_revise():
    original = "Your three utility bills total $1,240 — you can afford the repair."
    corrected = "Your three utility bills total $1,180. I can't say yet whether the repair fits."
    backend = two_phase(CLEAN_JSON, corrected)

    final, report = verify.verified_turn(None, [], original, backend)

    assert final == corrected
    assert report["revised"] is True
    assert report["verified"] is True
    assert report["verdict"] == "revise"
    assert report["severity"] == "high"
    assert len(report["problems"]) == 1
    # Critic first, then the revision — same backend, different system prompts.
    assert backend.calls[0] is verify.CRITIC_SYSTEM
    assert backend.calls[1] is verify.REVISION_SYSTEM


def test_verified_turn_revises_at_most_once():
    backend = two_phase(CLEAN_JSON, "Corrected: the total is $1,180.")

    verify.verified_turn(None, [], "The total is $1,240.", backend, max_revisions=1)

    assert backend.calls.count(verify.REVISION_SYSTEM) == 1


def test_verified_turn_skips_revision_when_max_revisions_is_zero():
    original = "The total is $1,240."
    backend = two_phase(CLEAN_JSON, "should never be used")

    final, report = verify.verified_turn(None, [], original, backend, max_revisions=0)

    assert final == original
    assert report["verdict"] == "revise"
    assert report["revised"] is False
    assert verify.REVISION_SYSTEM not in backend.calls


def test_verified_turn_skips_cheap_replies_entirely():
    backend = canned("should never be called")

    final, report = verify.verified_turn(None, [], "Got it, saved.", backend)

    assert final == "Got it, saved."
    assert report["verified"] is False
    assert report["verdict"] == "skipped"
    assert report["reason"] == "not_consequential"
    assert backend.calls == []


def test_verified_turn_respects_the_config_flag(monkeypatch):
    monkeypatch.setattr(verify, "VERIFY_REPLIES", False)
    backend = canned("should never be called")
    reply = "You should transfer $4,000 before the 15th."

    final, report = verify.verified_turn(None, [], reply, backend)

    assert final == reply
    assert report["verified"] is False
    assert report["reason"] == "disabled"
    assert backend.calls == []


def test_verified_turn_survives_a_critic_that_throws():
    reply = "I recommend cancelling the $180/mo service."

    final, report = verify.verified_turn(None, [], reply, exploding())

    assert final == reply
    assert report["revised"] is False
    assert report["verdict"] == "pass"
    assert "backend failed" in report["note"]


def test_verified_turn_survives_a_revision_that_throws():
    original = "The total is $1,240."

    def run(session, system, messages):
        if system is verify.CRITIC_SYSTEM:
            return CLEAN_JSON
        raise RuntimeError("revision backend exploded")

    final, report = verify.verified_turn(None, [], original, run)

    assert final == original
    assert report["verdict"] == "revise"
    assert report["revised"] is False
    assert report["reason"] == "revision_failed"


def test_verified_turn_keeps_the_original_when_the_revision_is_empty():
    original = "The total is $1,240."
    backend = two_phase(CLEAN_JSON, "   ")

    final, report = verify.verified_turn(None, [], original, backend)

    assert final == original
    assert report["revised"] is False
    assert report["reason"] == "revision_failed"


def test_verified_turn_never_raises_on_a_malformed_backend():
    # A "backend" with the wrong return type must not take down the turn.
    reply = "You should transfer $900 to savings."

    final, report = verify.verified_turn(None, [], reply, lambda s, sys, m: None)

    assert final == reply
    assert report["revised"] is False


def test_verified_turn_gives_the_reviser_the_problem_list():
    original = "Your utilities totalled $1,240 last month."
    seen = {}

    def run(session, system, messages):
        if system is verify.CRITIC_SYSTEM:
            return CLEAN_JSON
        seen["prompt"] = messages[-1]["content"]
        return "Your utilities totalled $1,180 last month."

    verify.verified_turn(None, [], original, run)

    assert "does not match the three line items" in seen["prompt"]
    assert original in seen["prompt"]
