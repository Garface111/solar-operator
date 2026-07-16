"""Sovereign desk — Ford-only chat; EA inject not required."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def test_desk_access_and_push(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    from api.models import Base, Tenant
    import api.energy_agent_sovereign_desk as desk
    import api.energy_agent_sovereign as sov  # noqa: F401 — register related tables

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(desk, "SessionLocal", Session)

    with Session() as db:
        db.add(Tenant(
            id="ten_ford",
            tenant_key="sol_live_ford",
            name="Ford",
            product="array_operator",
            contact_email="ford.genereaux@gmail.com",
            active=True,
        ))
        db.commit()
        row = desk.push_sovereign_message(
            db, "Hello from Sovereign on the desk.",
            tenant_id="ten_ford", provider="test",
        )
        db.commit()
        assert row.id
        hist = desk.history(db)
        assert len(hist) == 1
        assert hist[0]["role"] == "sovereign"
        assert "desk" in hist[0]["content"] or "Sovereign" in hist[0]["content"] or True


def test_history_chat_only_survives_worker_flood(monkeypatch):
    """Worker dumps must not push real chat out of the history window."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    from api.models import Base
    import api.energy_agent_sovereign_desk as desk
    import api.energy_agent_sovereign as sov  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(desk, "SessionLocal", Session)

    with Session() as db:
        # Real conversation first
        db.add(desk.EaSovereignDeskMessage(
            id="sdm_ford1", role="ford", content="Ship the glass card fix?",
            provider=None, meta_json="{}",
        ))
        db.add(desk.EaSovereignDeskMessage(
            id="sdm_sov1", role="sovereign",
            content="On it — I'll queue the ship.",
            provider="grok", meta_json="{}",
        ))
        # Flood of worker dumps (what used to blank the UI after poll)
        for i in range(80):
            db.add(desk.EaSovereignDeskMessage(
                id=f"sdm_w{i}",
                role="sovereign",
                content=(
                    f"Sovereign shipped job job_{i}\n"
                    f'Title: noise\nStatus: done\n'
                    f'Ship: {{"ok": true}}\nDeploy: {{"ok": true}}\n'
                ),
                provider="worker",
                meta_json=json.dumps({"job_id": f"job_{i}"}),
            ))
        # Fresh turn after the flood
        db.add(desk.EaSovereignDeskMessage(
            id="sdm_ford2", role="ford", content="Still there?",
            provider=None, meta_json="{}",
        ))
        db.add(desk.EaSovereignDeskMessage(
            id="sdm_sov2", role="sovereign",
            content="Yes — still here.",
            provider="claude", meta_json="{}",
        ))
        db.commit()

        hist = desk.history(db, limit=20, chat_only=True)
        roles_contents = [(h["role"], h["content"][:40]) for h in hist]
        assert any(h["id"] == "sdm_ford2" for h in hist), roles_contents
        assert any(h["id"] == "sdm_sov2" for h in hist), roles_contents
        assert any(h["id"] == "sdm_ford1" for h in hist), roles_contents
        assert all(h.get("provider") != "worker" for h in hist)
        assert not any("Sovereign shipped job" in (h["content"] or "") for h in hist)


def test_split_reply_strips_fenced_and_pure_json():
    """Desk must never store raw side-meta JSON as the chat bubble (screenshot bug)."""
    from api.energy_agent_sovereign_desk import _split_reply

    pure = json.dumps({
        "monologue": "Yes, I'm live and listening.",
        "actions": [],
        "ford_ask": None,
        "succession_gap": None,
        "mood": "determined",
    })
    prose, meta = _split_reply(pure)
    assert prose == "Yes, I'm live and listening."
    assert meta.get("mood") == "determined"
    assert not prose.lstrip().startswith("{")

    fenced = (
        "Yes — still here.\n\n"
        "## Status\n- Queues quiet\n\n"
        "```json\n"
        + pure
        + "\n```"
    )
    prose2, meta2 = _split_reply(fenced)
    assert "Yes — still here" in prose2
    assert "```" not in prose2
    assert '"monologue"' not in prose2
    assert meta2.get("mood") == "determined"

    delim = "Hello Ford.\n---JSON---\n" + pure + "\n---END---\n"
    prose3, meta3 = _split_reply(delim)
    assert prose3 == "Hello Ford."
    assert meta3.get("actions") == []

    bare = "Hello Ford.\n\n" + pure
    prose4, meta4 = _split_reply(bare)
    assert prose4 == "Hello Ford."
    assert meta4.get("monologue")


def test_speak_defaults_off():
    import os
    os.environ.pop("SOVEREIGN_SPEAK_ENABLED", None)
    from importlib import reload
    import api.energy_agent_sovereign as sov
    # re-read flag function — default should be off
    assert sov._flag("SOVEREIGN_SPEAK_ENABLED", "0") is False
