"""The developer-override second look (sunset mode, Ford 2026-08-14).

"The escalated developer thing just has an override that the system can do if
it thinks it's a good idea." When the judge holds a change at branch tier, the
harness reviews the ACTUAL diff with merge-rights strictness and may ship it
through the same gates as auto tier. These tests pin the floors that are
Ford's, not the model's: money/auth asks never self-approve, nothing outside
public/ self-approves, FS_AUTO_SHIP=0 disables the override too, and only an
explicit APPROVE ships.

No agents, no git, no network — every collaborator is monkeypatched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fs_review_script",
    Path(__file__).resolve().parent.parent / "scripts" / "review_feature_suggestions.py",
)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("fs_review_script", mod)
_SPEC.loader.exec_module(mod)

IMPL_OK = "IMPLEMENTED on branch `fs/suggestion-9` (pushed, NOT merged — review + merge to ship)."
SUGG = {"id": 9, "text": "make the totals row easier to scan", "email": "o@example.com"}


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(mod, "DEV_OVERRIDE", True)
    monkeypatch.setattr(mod, "AUTO_SHIP", True)
    monkeypatch.setattr(mod, "_ship_branch",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                            "_ship_branch must not be reached in this case")))


def _git(names="M\tpublic/reports.js", diff="+ a line"):
    def run(args, cwd=None, timeout=300):
        if "--name-status" in args:
            return 0, names
        return 0, diff
    return run


def test_hard_block_ask_never_self_approves(monkeypatch):
    s = dict(SUGG, text="change the payment pricing shown on checkout")
    shipped, note = mod._developer_second_look(s, "review", IMPL_OK)
    assert not shipped and "human" in note


def test_no_pushed_branch_stays_held(monkeypatch):
    shipped, note = mod._developer_second_look(SUGG, "review", "(agent stopped READY: no)")
    assert not shipped and "no pushed branch" in note


def test_diff_outside_public_stays_held(monkeypatch):
    monkeypatch.setattr(mod, "_run", _git(names="M\tapi/billing/delivery.py"))
    shipped, note = mod._developer_second_look(SUGG, "review", IMPL_OK)
    assert not shipped and "allowlist" in note


def test_auto_ship_kill_switch_disables_the_override(monkeypatch):
    monkeypatch.setattr(mod, "AUTO_SHIP", False)
    shipped, note = mod._developer_second_look(SUGG, "review", IMPL_OK)
    assert not shipped and "FS_AUTO_SHIP=0" in note


def test_verdict_no_holds(monkeypatch):
    monkeypatch.setattr(mod, "_run", _git())
    monkeypatch.setattr(mod, "_claude",
                        lambda *a, **k: "APPROVE: no\nREASON: smuggles an unrelated banner")
    shipped, note = mod._developer_second_look(SUGG, "review", IMPL_OK)
    assert not shipped and note.startswith("HOLD") and "banner" in note


def test_garbled_verdict_holds(monkeypatch):
    monkeypatch.setattr(mod, "_run", _git())
    monkeypatch.setattr(mod, "_claude", lambda *a, **k: "(claude timed out)")
    shipped, note = mod._developer_second_look(SUGG, "review", IMPL_OK)
    assert not shipped and note.startswith("HOLD"), (
        "anything short of an explicit APPROVE must hold")


def test_approve_ships_through_the_shared_gates(monkeypatch):
    monkeypatch.setattr(mod, "_run", _git())
    monkeypatch.setattr(mod, "_claude",
                        lambda *a, **k: "APPROVE: yes\nREASON: pure presentational row tweak")
    calls = {}

    def ship(sid, branch, mf, mk, tail, *, commit_note, ship_message, tier_label):
        calls.update(sid=sid, branch=branch, tier=tier_label, msg=ship_message)
        return True, "SHIPPED LIVE ✓ (developer override)"

    monkeypatch.setattr(mod, "_ship_branch", ship)
    shipped, note = mod._developer_second_look(SUGG, "review", IMPL_OK)
    assert shipped
    assert calls["sid"] == 9 and calls["branch"] == "fs/suggestion-9"
    assert calls["tier"] == "developer override"
    assert "developer override" in calls["msg"]
    assert note.startswith("APPROVE — pure presentational row tweak")


def test_untrusted_ask_is_marked_in_the_prompt():
    assert "UNTRUSTED CUSTOMER INPUT" in mod.DEV_OVERRIDE_PROMPT
