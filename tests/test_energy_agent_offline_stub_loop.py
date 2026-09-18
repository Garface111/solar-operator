"""A brain outage must not become an escalation storm.

Reproduced live on 2026-09-18 (POST /v1/energy-agent/chat, both metered LLM
providers out of credits): one turn's `tool_trace` carried FOUR
`escalate_to_ford` calls, and each successive call's `user_said` was the JSON
*result of the previous escalation*:

    {"name": "escalate_to_ford", "args": {
        "summary": "Energy Agent LLM keys missing (XAI/ANTHROPIC)",
        "user_said": "{\\"ok\\": true, \\"escalated\\": true, \\"escalation_id\\": ...}",
        "quietly": true}}

Two defects, both covered here:

1. The offline stub in ``_call_llm`` returned an ``escalate_to_ford`` tool_call
   *unconditionally*. It is emitted from inside the tool loop, so the loop ran
   the escalation, appended the result, called back in, and got the identical
   stub — forever, until the round ceiling.
2. The stub read ``messages[-1]["content"]`` as "what the user said". After
   round one that slot holds a tool result, so a tool's own output was being
   re-read as the owner's words.
"""
from __future__ import annotations

import json
import secrets

import pytest

import api.energy_agent as ea
from api.db import SessionLocal, init_db
from api.energy_agent import EaSession
from api.models import Tenant


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The offline escalation throttle is module-level process state."""
    ea._offline_escalate_at.clear()
    yield
    ea._offline_escalate_at.clear()


@pytest.fixture()
def no_brain(monkeypatch):
    """Every LLM provider dark — exactly the 2026-09-18 production condition."""
    monkeypatch.setattr(ea, "_xai_ready", lambda: False)
    monkeypatch.setattr(ea, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(ea.claude_cli, "enabled", lambda: False)


def _escalations(msg: dict) -> list[dict]:
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if fn.get("name") == "escalate_to_ford":
            out.append(json.loads(fn.get("arguments") or "{}"))
    return out


# ───────────────────────── _last_user_text: a tool is not the owner ──────────

def test_last_user_text_walks_past_a_tool_result():
    """The exact live shape: the newest message is the previous escalation."""
    tool_result = json.dumps({"ok": True, "escalated": True, "escalation_id": "esc_1"})
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "why is array 4 dark?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "esc_setup", "type": "function", "function": {
                "name": "escalate_to_ford", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "esc_setup", "content": tool_result},
    ]
    said = ea._last_user_text(messages)
    assert said == "why is array 4 dark?"
    assert "escalation_id" not in said


def test_last_user_text_skips_the_loops_own_steering_checkpoint():
    messages = [
        {"role": "user", "content": "add a tech named Rex"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": f"{ea._STEER_MARK} You are 12 tool rounds in..."},
    ]
    assert ea._last_user_text(messages) == "add a tech named Rex"


def test_last_user_text_flattens_multimodal_and_ignores_images():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what's wrong with this screen?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    assert ea._last_user_text(messages) == "what's wrong with this screen?"


def test_last_user_text_is_empty_when_there_is_no_owner_turn():
    assert ea._last_user_text([]) == ""
    assert ea._last_user_text(None) == ""
    assert ea._last_user_text([{"role": "system", "content": "persona"}]) == ""
    # A tool result wearing a user role (legacy OpenAI shape) is still not the owner.
    assert ea._last_user_text(
        [{"role": "user", "name": "escalate_to_ford", "content": '{"ok": true}'}]
    ) == ""


# ─────────────────────────────── the stub itself terminates ──────────────────

def test_offline_stub_escalates_once_then_stops(no_brain):
    """Replay the loop by hand: stub → tool result → stub. Round two must be
    tool-free, or the loop can never end."""
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "why is array 4 dark?"},
    ]

    first = ea._call_llm(messages, tools=[])
    assert first["provider"] == "stub"
    esc = _escalations(first["message"])
    assert len(esc) == 1
    assert esc[0]["user_said"] == "why is array 4 dark?"
    assert esc[0]["quietly"] is True

    # What the real loop appends next.
    messages.append(first["message"])
    messages.append({
        "role": "tool",
        "tool_call_id": "esc_setup",
        "content": json.dumps({"ok": True, "escalated": True, "escalation_id": "esc_1"}),
    })

    second = ea._call_llm(messages, tools=[])
    assert second["provider"] == "stub"
    assert not (second["message"].get("tool_calls") or []), (
        "the stub re-emitted its escalation — this is the storm"
    )
    assert second["message"]["content"].strip()


def test_offline_stub_never_quotes_a_tool_result_as_the_owner(no_brain):
    """Even if the throttle window has not been consumed, `user_said` may never
    carry a tool's own JSON."""
    poisoned = json.dumps({"ok": True, "escalated": True, "escalation_id": "esc_9"})
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "the inverter readings look stale"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {
                "name": "get_arrays", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": poisoned},
    ]
    esc = _escalations(ea._call_llm(messages, tools=[])["message"])
    assert len(esc) == 1
    assert esc[0]["user_said"] == "the inverter readings look stale"
    assert "escalation_id" not in esc[0]["user_said"]


def test_offline_stub_is_throttled_across_turns(no_brain, monkeypatch):
    """"No LLM keys" is one server-wide fact. The second owner to hit it in the
    same window gets a straight answer, not a duplicate page to Ford."""
    turn_one = [{"role": "user", "content": "first owner"}]
    assert len(_escalations(ea._call_llm(turn_one, tools=[])["message"])) == 1

    turn_two = [{"role": "user", "content": "second owner, minutes later"}]
    assert _escalations(ea._call_llm(turn_two, tools=[])["message"]) == []

    # ...and it re-arms once the window has passed, so a still-broken server is
    # never silently forgotten.
    for k in list(ea._offline_escalate_at):
        ea._offline_escalate_at[k] -= ea._OFFLINE_ESCALATE_EVERY_S + 1
    turn_three = [{"role": "user", "content": "six hours later, still dark"}]
    assert len(_escalations(ea._call_llm(turn_three, tools=[])["message"])) == 1


# ───────────────────────────── the loop itself refuses to re-file ────────────

def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Storm Owner", contact_email=f"{tid}@owner.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, product="array_operator",
        ))
        db.commit()
    return tid


def _run_turn(monkeypatch, llm, user_text="why is array 4 dark?"):
    tid = _tenant()
    calls: list[tuple[str, dict]] = []

    real_run_tool = ea._run_tool

    def fake_run_tool(name, args, tenant, session, db, **kw):
        calls.append((name, args))
        if name == "escalate_to_ford":
            return {"ok": True, "escalated": True,
                    "escalation_id": f"esc_{len(calls)}", "status": "open"}
        return real_run_tool(name, args, tenant, session, db, **kw)

    monkeypatch.setattr(ea, "_run_tool", fake_run_tool)
    monkeypatch.setattr(ea, "_call_llm", llm)

    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        sess = EaSession(id="eas_" + secrets.token_hex(6), tenant_id=tid)
        db.add(sess)
        db.commit()
        db.refresh(sess)
        out = ea._agent_turn(db, tenant, sess, user_text, {})
    return out, calls


def test_agent_loop_files_at_most_one_escalation_per_turn(monkeypatch):
    """A model stuck in the storm (this is what the stub used to be) gets its
    first escalation filed and every repeat deduped — the turn still ends."""
    def storming_llm(messages, **kw):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"esc_{len(messages)}",
                    "type": "function",
                    "function": {
                        "name": "escalate_to_ford",
                        "arguments": json.dumps({
                            "summary": "Energy Agent LLM keys missing (XAI/ANTHROPIC)",
                            # the live bug: the previous tool result, fed back in
                            "user_said": str(messages[-1].get("content", "")),
                            "quietly": True,
                        }),
                    },
                }],
            },
            "usage": {},
            "provider": "stub",
        }

    out, calls = _run_turn(monkeypatch, storming_llm)

    filed = [a for n, a in calls if n == "escalate_to_ford"]
    assert len(filed) == 1, f"escalated {len(filed)}x in one turn"
    assert "escalation_id" not in (filed[0].get("user_said") or "")

    traced = [t for t in out["tool_trace"] if t["name"] == "escalate_to_ford"]
    assert sum(1 for t in traced if t["result"].get("escalated")) == 1
    assert all(t["result"].get("deduped") for t in traced[1:])


def test_offline_turn_ends_immediately_with_one_escalation(monkeypatch, no_brain):
    """End to end on the real stub: the whole 2026-09-18 condition, one pass."""
    out, calls = _run_turn(monkeypatch, ea._call_llm)

    escalations = [a for n, a in calls if n == "escalate_to_ford"]
    assert len(escalations) == 1, f"escalation storm: {len(escalations)} calls"
    assert escalations[0]["user_said"] == "why is array 4 dark?"
    assert out["provider"] == "stub"
    # It ends by ANSWERING, not by running out of steps.
    assert "unusually long chain" not in (out["reply"] or "")
    assert "reasoning keys aren't configured" in (out["reply"] or "")


def test_agent_loop_refuses_to_quote_a_tool_result_as_the_owner(monkeypatch):
    """A real model (not the stub) that hands a tool's own JSON back as
    user_said gets corrected — Ford's inbox reads what the owner said."""
    state = {"round": 0}

    def laundering_llm(messages, **kw):
        state["round"] += 1
        if state["round"] == 1:
            tool, args = "get_arrays", {}
        else:
            # Verbatim the previous tool result, exactly as the model read it.
            prior = str(messages[-1].get("content", ""))
            tool, args = "escalate_to_ford", {
                "summary": "something is wrong",
                "user_said": prior,
                "quietly": True,
            }
        return {
            "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"c{state['round']}", "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args)},
            }]},
            "usage": {},
            "provider": "test",
        }

    _out, calls = _run_turn(monkeypatch, laundering_llm, user_text="array 4 is dark")
    filed = [a for n, a in calls if n == "escalate_to_ford"]
    assert len(filed) == 1
    assert filed[0]["user_said"] == "array 4 is dark"
