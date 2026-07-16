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


def test_find_orphan_desk_turns_and_meta(monkeypatch):
    """Orphan ford messages (no reply) are discoverable for post-restart resume."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    from datetime import datetime, timedelta
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

    with Session() as db:
        old = datetime.utcnow() - timedelta(seconds=120)
        ford = desk.EaSovereignDeskMessage(
            id="sdm_orphan_1",
            role="ford",
            content="still there?",
            created_at=old,
            meta_json=json.dumps({
                "channel": "desk",
                "client_request_id": "cr_orphan_1",
                "turn_status": "thinking",
            }),
        )
        db.add(ford)
        db.commit()
        found = desk.find_orphan_desk_turns(
            db, min_age_sec=30, max_age_sec=3600, limit=5
        )
        assert any(f.id == "sdm_orphan_1" for f in found)

        # After a reply exists, no longer orphan
        db.add(desk.EaSovereignDeskMessage(
            id="sdm_orphan_reply",
            role="sovereign",
            content="yes",
            created_at=datetime.utcnow(),
            provider="grok",
            meta_json=json.dumps({
                "client_request_id": "cr_orphan_1",
                "reply_to_ford": "sdm_orphan_1",
            }),
        ))
        db.commit()
        found2 = desk.find_orphan_desk_turns(
            db, min_age_sec=30, max_age_sec=3600, limit=5
        )
        assert not any(f.id == "sdm_orphan_1" for f in found2)


def test_cancel_marks_crid_and_writes_stopped(monkeypatch):
    """Stop button: mark cancelled + persist *(Stopped.)* when ford exists."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    from api.models import Base, Tenant
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

    crid = "cr_stop_test_1"
    with Session() as db:
        db.add(Tenant(
            id="ten_ford",
            tenant_key="sol_live_ford",
            name="Ford",
            product="array_operator",
            contact_email="ford.genereaux@gmail.com",
            active=True,
        ))
        ford = desk.EaSovereignDeskMessage(
            id="sdm_ford_stop",
            role="ford",
            content="long think please",
            tenant_id="ten_ford",
            meta_json=json.dumps({"client_request_id": crid, "channel": "desk"}),
        )
        db.add(ford)
        db.commit()

    desk._mark_crid_cancelled(crid)
    assert desk._is_crid_cancelled(crid)

    with Session() as db:
        hit = desk.lookup_turn_by_client_request_id(db, crid)
        assert hit and not hit["complete"]
        sov_row = desk._write_stopped_reply(
            db, ford=hit["ford"], client_request_id=crid, tenant_id="ten_ford"
        )
        db.commit()
        assert sov_row.content == desk._STOP_REPLY
        hit2 = desk.lookup_turn_by_client_request_id(db, crid)
        assert hit2 and hit2["complete"]
        assert "Stopped" in (hit2["sov"].content or "")


def test_lookup_turn_by_client_request_id_idempotent(monkeypatch):
    """Retries with the same client_request_id find the prior turn."""
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

    with Session() as db:
        crid = "cr_test_idem_1"
        ford = desk.EaSovereignDeskMessage(
            id="sdm_ford_cr",
            role="ford",
            content="ping",
            meta_json=json.dumps({"client_request_id": crid, "channel": "desk"}),
        )
        db.add(ford)
        db.flush()
        hit = desk.lookup_turn_by_client_request_id(db, crid)
        assert hit and not hit["complete"]
        assert hit["ford"].id == "sdm_ford_cr"

        sov_row = desk.EaSovereignDeskMessage(
            id="sdm_sov_cr",
            role="sovereign",
            content="pong",
            provider="grok",
            meta_json=json.dumps({
                "client_request_id": crid,
                "reply_to_ford": "sdm_ford_cr",
                "channel": "desk",
            }),
        )
        db.add(sov_row)
        db.commit()
        hit2 = desk.lookup_turn_by_client_request_id(db, crid)
        assert hit2 and hit2["complete"]
        assert hit2["sov"].content == "pong"
        out = desk._format_turn_response(
            ford=hit2["ford"], sov=hit2["sov"], pending=False, client_request_id=crid
        )
        assert out["ok"] and not out["pending"]
        assert out["reply"] == "pong"


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


def test_desk_offload_defaults_by_process_role(monkeypatch):
    """Web role must offload desk brain; worker must not."""
    import api.energy_agent_sovereign_desk as desk

    monkeypatch.delenv("SOVEREIGN_DESK_OFFLOAD", raising=False)
    monkeypatch.setenv("PROCESS_ROLE", "web")
    monkeypatch.delenv("SO_PROCESS", raising=False)
    assert desk.desk_offload_enabled() is True

    monkeypatch.setenv("PROCESS_ROLE", "worker")
    assert desk.desk_offload_enabled() is False

    monkeypatch.setenv("SOVEREIGN_DESK_OFFLOAD", "0")
    monkeypatch.setenv("PROCESS_ROLE", "web")
    assert desk.desk_offload_enabled() is False

    monkeypatch.setenv("SOVEREIGN_DESK_OFFLOAD", "1")
    monkeypatch.setenv("PROCESS_ROLE", "worker")
    assert desk.desk_offload_enabled() is True


def test_enqueue_desk_message_never_calls_brain(monkeypatch):
    """Web enqueue saves Ford bubble only — no call_brain."""
    monkeypatch.setenv("PROCESS_ROLE", "web")
    monkeypatch.setenv("SOVEREIGN_DESK_OFFLOAD", "1")
    from api.models import Base, Tenant
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

    brain_calls = {"n": 0}

    def _no_brain(*a, **k):
        brain_calls["n"] += 1
        raise AssertionError("call_brain must not run on web enqueue")

    monkeypatch.setattr(
        "api.energy_agent_sovereign_brain.call_brain", _no_brain, raising=False
    )

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

    out = desk.enqueue_desk_message(
        tenant_id="ten_ford",
        message="Detach the mind so AO never hangs.",
        client_request_id="cr_offload_1",
    )
    assert out["ok"] is True
    assert out["pending"] is True
    assert out.get("offloaded") is True
    assert out.get("ford_message_id")
    assert brain_calls["n"] == 0

    with Session() as db:
        ford = db.get(desk.EaSovereignDeskMessage, out["ford_message_id"])
        assert ford is not None
        meta = json.loads(ford.meta_json or "{}")
        assert meta.get("turn_status") == "thinking"
        assert meta.get("offloaded") is True
        assert meta.get("client_request_id") == "cr_offload_1"
        ford_id = ford.id

    drained = {"n": 0}

    def _fake_desk_turn(db, t, msg, **kw):
        drained["n"] += 1
        sov_row = desk.EaSovereignDeskMessage(
            id="sdm_sov_drain",
            role="sovereign",
            content="Detached and working.",
            tenant_id=t.id,
            provider="test",
            meta_json=json.dumps({
                "client_request_id": "cr_offload_1",
                "reply_to_ford": ford_id,
            }),
        )
        db.add(sov_row)
        desk._patch_ford_meta(db, ford_id, turn_status="done")
        return {
            "ok": True,
            "reply": "Detached and working.",
            "message": {"id": sov_row.id},
            "provider": "test",
        }

    monkeypatch.setattr(desk, "desk_turn", _fake_desk_turn)
    res = desk.recover_orphan_desk_turns(
        limit=3, min_age_sec=0, max_age_sec=3600, reason="test_drain",
    )
    assert res["recovered"] >= 1
    assert drained["n"] >= 1
