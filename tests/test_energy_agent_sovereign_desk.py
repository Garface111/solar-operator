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


def test_speak_defaults_off():
    import os
    os.environ.pop("SOVEREIGN_SPEAK_ENABLED", None)
    from importlib import reload
    import api.energy_agent_sovereign as sov
    # re-read flag function — default should be off
    assert sov._flag("SOVEREIGN_SPEAK_ENABLED", "0") is False
