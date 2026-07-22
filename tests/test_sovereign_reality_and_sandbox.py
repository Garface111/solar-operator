"""Reality file + mind sandbox — no DB / no LLM required."""
from __future__ import annotations

import json
from pathlib import Path

import api.energy_agent_sovereign_reality as reality
import api.energy_agent_sovereign_mind_sandbox as sandbox


def test_append_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(reality, "SO_ROOT", tmp_path)
    monkeypatch.setattr(reality, "REALITY_DIR", tmp_path / "docs" / "sovereign" / "reality")
    monkeypatch.setattr(reality, "CHANGELOG_PATH", reality.REALITY_DIR / "CHANGELOG.jsonl")
    monkeypatch.setattr(reality, "INDEX_PATH", reality.REALITY_DIR / "INDEX.md")
    monkeypatch.setattr(reality, "README_PATH", reality.REALITY_DIR / "README.md")

    r = reality.append_entry(
        summary="Add fleet ask button",
        source="ford",
        repos=["array-operator"],
        files=["public/command-center.js", "public/energy-agent.js"],
        why="Command Center AI slice",
    )
    assert r["ok"]
    assert reality.entry_count() == 1
    entries = reality.read_entries(limit=5)
    assert entries[0]["summary"] == "Add fleet ask button"
    assert "frontend" in entries[0]["surfaces"]

    # duplicate sha skipped
    r2 = reality.append_entry(
        summary="dup",
        source="git",
        repos=["array-operator"],
        sha="abc123",
    )
    r3 = reality.append_entry(
        summary="dup2",
        source="git",
        repos=["array-operator"],
        sha="abc123",
    )
    assert r3.get("skipped") == "duplicate_sha"
    assert reality.entry_count() == 2

    wake = reality.load_for_wake(tail=10)
    assert wake["total_entries"] == 2
    assert "REALITY FILE" in wake["doctrine"]
    assert wake["recent_changes"]


def test_classify_surfaces():
    assert "frontend" in reality.classify_surfaces(["public/app.js"])
    assert "backend" in reality.classify_surfaces(["api/energy_agent.py"])
    assert "extension" in reality.classify_surfaces(["extension/content.js"])


def test_sandbox_start_score_end(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "SANDBOX_ROOT", tmp_path / "sbox")
    monkeypatch.setattr(sandbox, "RUNS_META", tmp_path / "meta")
    monkeypatch.setattr(sandbox, "SO_ROOT", tmp_path)
    monkeypatch.setattr(sandbox, "AO_ROOT", tmp_path / "missing-ao")

    # avoid real worktrees
    monkeypatch.setattr(sandbox, "_prepare_worktrees", lambda rid: {})

    out = sandbox.start_run(None, days=7, title="Week 1 free-run", goal="Beat Ford")
    assert out["ok"]
    rid = out["run"]["id"]
    assert out["run"]["status"] == "open"

    active = sandbox.get_active_run(None)
    assert active and active["id"] == rid

    sandbox.register_sandbox_job(
        None,
        rid,
        job_id="job_test",
        title="sandbox polish",
        repo="array-operator",
        result={"ok": True, "sha": "deadbeef", "files": ["public/x.js"]},
    )
    card = sandbox.score_active(None, run_id=rid)
    assert card["ok"]
    assert card["scorecard"]["counts"]["sovereign_jobs"] == 1

    ended = sandbox.end_run(None, run_id=rid, score=True)
    assert ended["ok"]
    assert ended["run"]["status"] == "closed"
    assert (tmp_path / "sbox" / rid / "comparison.md").exists()


def test_build_think_prompt_includes_reality():
    from api.energy_agent_sovereign_brain import build_think_prompt

    msgs = build_think_prompt(
        digests={},
        world={},
        goals=[],
        recent_notes=[],
        memories=[],
        open_jobs=[],
        reality={
            "doctrine": "REALITY FILE is cold hard truth",
            "total_entries": 3,
            "recent_changes": [{"summary": "x", "source": "ford"}],
        },
        mind_sandbox={"active": True, "run_id": "sbox_test", "doctrine": "sandbox open"},
    )
    blob = msgs[1]["content"]
    assert "reality_file" in blob
    assert "REALITY FILE is cold hard truth" in blob
    assert "mind_sandbox" in blob
    assert "sbox_test" in blob
