"""The 'inverter_down' watch — the owner-defined "tell me if an inverter goes
down" reminder — narrated comm_gap (data feed went quiet: a lapsed utility
login, a stalled capture) with the exact same "just went down" wording it uses
for dead/fault (a real hardware failure). api/jobs/morning_fleet_digest.py
already protects this exact distinction ("we can't see it" is NOT "it went
dark" — Paul Bozuwa, 2026-07-19: a customer was told an array "went dark
overnight" when it had never produced, while the real event, a different
site's utility session dying, went unmentioned). _DOWN_STATUSES = ("dead",
"fault", "comm_gap") in this watch flattened that same distinction back out.

_reminder_fleet_state now also builds status_by_id (ephemeral — never
persisted to armed_json, so no baseline-format migration needed) so
_evaluate_reminder / _reminder_initial_note can speak honestly per-id instead
of collapsing every down_ids member into one "just went down" sentence.
"""
from __future__ import annotations

import json

from api.energy_agent import EaReminder, _evaluate_reminder, _reminder_initial_note


def _rem(armed_down_ids=()) -> EaReminder:
    return EaReminder(
        id="rem_test", tenant_id="ten_test", watch_type="inverter_down",
        params_json="{}",
        armed_json=json.dumps({"down_ids": list(armed_down_ids)}),
    )


def test_hard_failure_alone_keeps_the_original_wording():
    state = {
        "down_ids": {"a1:INV-1"}, "down_by_array": {},
        "status_by_id": {"a1:INV-1": "dead"},
        "arrays": {}, "name_to_aid": {},
    }
    fired, detail = _evaluate_reminder(_rem(), state)
    assert fired is True
    assert "just went down" in detail
    assert "INV-1" in detail
    assert "quiet" not in detail


def test_comm_gap_alone_gets_honest_wording_not_went_down():
    state = {
        "down_ids": {"a1:INV-2"}, "down_by_array": {},
        "status_by_id": {"a1:INV-2": "comm_gap"},
        "arrays": {}, "name_to_aid": {},
    }
    fired, detail = _evaluate_reminder(_rem(), state)
    assert fired is True
    assert "went quiet" in detail
    assert "INV-2" in detail
    assert "just went down" not in detail


def test_mixed_hard_and_comm_gap_gets_both_clauses_distinctly():
    state = {
        "down_ids": {"a1:INV-1", "a1:INV-2"}, "down_by_array": {},
        "status_by_id": {"a1:INV-1": "dead", "a1:INV-2": "comm_gap"},
        "arrays": {}, "name_to_aid": {},
    }
    fired, detail = _evaluate_reminder(_rem(), state)
    assert fired is True
    assert "just went down" in detail and "INV-1" in detail
    assert "went quiet" in detail and "INV-2" in detail


def test_initial_note_distinguishes_down_from_quiet():
    state = {
        "down_ids": {"a1:INV-1", "a1:INV-2"},
        "down_by_array": {"a1": {"a1:INV-1", "a1:INV-2"}},
        "status_by_id": {"a1:INV-1": "dead", "a1:INV-2": "comm_gap"},
        "arrays": {"a1": {"name": "West Glover", "needs_attention": True}},
        "name_to_aid": {"west glover": "a1"},
    }
    note = _reminder_initial_note(_rem(), state)
    assert "1 inverter(s) down" in note
    assert "1 gone quiet (no data)" in note
