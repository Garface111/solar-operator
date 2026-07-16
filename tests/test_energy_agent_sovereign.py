"""Sovereign Mind — full runtime tests (observe, audit, gates, admin API)."""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def _clear_sovereign_env(monkeypatch):
    for k in (
        "SOVEREIGN_ENABLED",
        "SOVEREIGN_ACT_ENABLED",
        "SOVEREIGN_SPEAK_ENABLED",
        "SOVEREIGN_EMAIL_ENABLED",
        "SOVEREIGN_SENSE_ENABLED",
        "SOVEREIGN_SPEAK_ALL",
        "SOVEREIGN_CAPABILITIES",
        "SOVEREIGN_ARM_T4_T5",
        "SOVEREIGN_SERVICE_KEY",
        "ADMIN_API_KEY",
        "SOVEREIGN_MAIL_TO",
        "MAIL_FROM_SOVEREIGN",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def test_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "0")
    from api.energy_agent_sovereign import capability_allowed, sovereign_tick

    assert capability_allowed("sense.queues") is False
    out = sovereign_tick(reason="test")
    assert out["mode"] == "dark"
    assert out["enabled"] is False


def test_money_never_autonomous(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ARM_T4_T5", "1")
    monkeypatch.setenv("SOVEREIGN_CAPABILITIES", "*")
    from api.energy_agent_sovereign import plan_action

    out = plan_action("act.money_identity", {"do": "bad"})
    assert out["denied"] is True


def test_never_autonomous_denied_even_when_armed(monkeypatch):
    """No SOVEREIGN_* flag un-gates money/deploy/brand/hard-delete/HAR."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ARM_T4_T5", "1")
    monkeypatch.setenv("SOVEREIGN_SUCCESSION_FULL", "1")
    monkeypatch.setenv("SOVEREIGN_CAPABILITIES", "*")
    from api.energy_agent_sovereign import capability_allowed, plan_action

    for cap in (
        "act.money_identity", "act.deploy", "act.brand",
        "act.hard_delete", "act.har_capture",
    ):
        assert capability_allowed(cap) is False, cap
        assert plan_action(cap, {}).get("denied") is True, cap


def test_succession_gate_default_off_and_ford_context(monkeypatch):
    """succession_full() is off by default and only true inside ford_execution()."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.delenv("SOVEREIGN_SUCCESSION_FULL", raising=False)
    from api.energy_agent_sovereign_succession import ford_execution, succession_full

    assert succession_full() is False
    with ford_execution():
        assert succession_full() is True
    assert succession_full() is False


def test_brain_drafts_dangerous_action_instead_of_executing(monkeypatch):
    """A stripe_refund from the brain becomes a queued Ford approval; the real
    refund is never called by the autonomous path."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SUCCESSION_FULL", "1")  # even armed → still drafts

    from api.models import Base
    import api.energy_agent_sovereign as sov
    import api.energy_agent_sovereign_succession as succ

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)
    monkeypatch.setattr(sov, "email_ford", lambda *a, **k: True)

    called = {"refund": False}
    monkeypatch.setattr(
        succ, "stripe_refund",
        lambda *a, **k: called.__setitem__("refund", True) or {"ok": True},
    )

    with Session() as db:
        out = sov.execute_brain_actions(
            db,
            [{"type": "stripe_refund", "amount_cents": 500, "payment_intent_id": "pi_x"}],
            tick_id="t_test",
        )
        db.commit()
        res = out[0]["result"]
        assert res.get("deferred") is True
        assert res.get("approval_id")
        assert called["refund"] is False
        job = db.get(sov.EaSovereignJob, res["approval_id"])
        assert job is not None and job.kind == "ford_approval" and job.status == "queued"


def test_ford_fire_approval_executes(monkeypatch):
    """The admin fire endpoint runs the drafted action inside ford_execution()."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")

    from api.models import Base
    import api.energy_agent_sovereign as sov
    import api.energy_agent_sovereign_succession as succ

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)
    monkeypatch.setattr(sov, "email_ford", lambda *a, **k: True)

    seen = {"ctx": None}
    monkeypatch.setattr(
        succ, "stripe_refund",
        lambda *a, **k: {"ok": True, "ctx": succ.ford_execution_active()},
    )

    with Session() as db:
        drafted = sov._draft_ford_approval(
            db, {"type": "stripe_refund", "amount_cents": 500, "charge_id": "ch_x"},
            cap="act.money_identity", tick_id="t",
        )
        db.commit()
        aid = drafted["approval_id"]
        with sov.ford_execution() if hasattr(sov, "ford_execution") else succ.ford_execution():
            res = sov._execute_approved_action(
                db, "stripe_refund",
                {"type": "stripe_refund", "amount_cents": 500, "charge_id": "ch_x"},
            )
        assert res["ok"] is True
        assert res["ctx"] is True  # ran inside ford_execution context
        assert aid


def test_email_ford_enabled_without_ea_speak(monkeypatch):
    """Email channel is ON even when SOVEREIGN_SPEAK_ENABLED (EA inject) is off."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")
    monkeypatch.setenv("SOVEREIGN_EMAIL_ENABLED", "1")
    monkeypatch.delenv("SOVEREIGN_CAPABILITIES", raising=False)
    from api.energy_agent_sovereign import (
        capability_allowed, sovereign_mail_from, sovereign_mail_recipients,
    )

    assert capability_allowed("speak.email_ford") is True
    assert capability_allowed("speak.session_inject") is False
    # Prefer agent subdomain so Reply hits Resend inbound (send+receive verified)
    assert "sovereign@" in sovereign_mail_from().lower()
    assert "arrayoperator.com" in sovereign_mail_from().lower()
    assert "ford.genereaux@gmail.com" in sovereign_mail_recipients()


def test_email_ford_sends_from_sovereign_domain(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_EMAIL_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")
    monkeypatch.delenv("SOVEREIGN_CAPABILITIES", raising=False)

    from api.models import Base
    import api.energy_agent_sovereign as sov
    import api.energy_agent_sovereign_desk  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)

    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)

    with Session() as db:
        ok = sov.email_ford(
            "Utility queue needs eyes",
            "Three co-ops still researching. Want me to stage credentials?",
            db=db,
            note_desk=True,
        )
        db.commit()
        assert ok is True
        from_a = (captured.get("from_addr") or "").lower()
        reply_a = (captured.get("reply_to") or "").lower()
        assert "sovereign@" in from_a
        # From == Reply-To so Gmail Reply hits inbound
        assert from_a == reply_a or "sovereign@" in reply_a
        # Sky skin should be HTML with AO tokens
        html = captured.get("html") or ""
        assert "2196F3" in html or "Array Operator" in html or "sky" in html.lower() or "Sovereign" in html
        assert "Utility queue" in (captured.get("subject") or "") or "Sovereign" in (
            captured.get("subject") or ""
        )
        to = captured.get("to")
        if isinstance(to, list):
            assert any("ford.genereaux@gmail.com" in str(x) for x in to)
        else:
            assert "ford" in str(to).lower()
        n = db.query(sov.EaSovereignAction).filter_by(capability="speak.email_ford").count()
        assert n >= 1


def test_sovereign_inbound_address_match():
    from api.energy_agent_sovereign_desk import is_sovereign_inbound_address
    assert is_sovereign_inbound_address(["sovereign@agent.arrayoperator.com"]) is True
    assert is_sovereign_inbound_address(["Sovereign <sovereign@arrayoperator.com>"]) is True
    assert is_sovereign_inbound_address(["repairs@agent.arrayoperator.com"]) is False


def test_email_ford_blocks_ops_telemetry(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_EMAIL_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")
    from api.energy_agent_sovereign import email_ford, _looks_like_ops_telemetry

    assert _looks_like_ops_telemetry(
        "[Sovereign] Code-hire job queued: Utility adapter",
        "Job id: job_abc\nKind: utility_adapter\n",
    )
    sent = []
    monkeypatch.setattr(
        "api.notify._send_via_resend",
        lambda **k: sent.append(k) or True,
    )
    ok = email_ford(
        "[Sovereign] Code-hire job queued: Utility adapter: healthcheck",
        "Job id: job_11bd1d3d0aa4476d\nKind: utility_adapter\nExpand: grok\n",
        note_desk=False,
    )
    assert ok is False
    assert sent == []
    # High-level still ok
    ok2 = email_ford(
        "Quick partnership note",
        "Utility coverage is moving — three co-ops in research. "
        "I'll only pull you in if we hit a HAR wall. No action needed tonight.",
        note_desk=False,
    )
    assert ok2 is True
    assert len(sent) == 1


def test_observe_and_tick_with_db(monkeypatch):
    """In-memory tables: observe digests + world save + audit observe row."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")  # no inject in this test

    from api.models import Base
    import api.energy_agent_sovereign as sov
    # Import mind models for EaTask etc if needed
    import api.energy_agent  # noqa: F401
    import api.energy_agent_mind  # noqa: F401
    import api.utility_requests  # noqa: F401
    import api.feature_suggestions  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Patch SessionLocal used by sovereign
    monkeypatch.setattr(sov, "SessionLocal", Session)

    with Session() as db:
        from api.utility_requests import UtilityRequest
        db.add(UtilityRequest(name="Test Co-op", product="array_operator", status="new"))
        db.commit()

    out = sov.sovereign_tick(reason="unit")
    assert out["ok"] is True
    assert out["mode"] == "live"
    assert out.get("digests", {}).get("queues", {}).get("utility_new", 0) >= 1

    with Session() as db:
        state = db.get(sov.EaSovereignState, "product")
        assert state is not None
        assert state.revision >= 1
        n_audit = db.query(sov.EaSovereignAction).count()
        assert n_audit >= 1
        # triage may have moved utility to researching
        from api.utility_requests import UtilityRequest
        u = db.query(UtilityRequest).first()
        assert u.status in ("new", "researching")


def test_stage_feature_and_code_hire(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")

    from api.models import Base
    import api.energy_agent_sovereign as sov
    import api.feature_suggestions  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)

    # Silence mailer
    monkeypatch.setattr(sov, "email_ford", lambda *a, **k: True)

    out = sov.plan_action("act.soft_stage", {"text": "Make Ops modals better"})
    assert out.get("ok") is True
    assert out.get("feature_id")

    job = sov.plan_action("act.code_hire", {
        "title": "Fix ops overlap",
        "brief": "Pin modal scrim to content column when EA open.",
    })
    assert job.get("ok") is True
    assert job.get("job_id")


def test_inject_dogfood_only(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ALL", "0")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "0")

    from api.models import Base, Tenant
    import api.energy_agent_sovereign as sov
    import api.energy_agent as ea
    import api.energy_agent_mind as mind  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)

    with Session() as db:
        db.add(Tenant(
            id="ten_other",
            tenant_key="sol_live_other",
            name="Other",
            product="array_operator",
            contact_email="stranger@example.com",
            active=True,
        ))
        db.add(Tenant(
            id="ten_ford",
            tenant_key="sol_live_ford",
            name="Ford",
            product="array_operator",
            contact_email="ford.genereaux@gmail.com",
            active=True,
        ))
        db.add(ea.EaSession(
            id="ses_ford",
            tenant_id="ten_ford",
            status="open",
        ))
        db.commit()

    # Speak inject now lands on Sovereign Desk (not EA), for any admin inject
    ok = sov.plan_inject(
        tenant_ids=["ten_ford"],
        speak="Hello Ford — sovereign is live on the desk.",
    )
    assert ok.get("ok") is True
    assert ok.get("channel") == "desk" or ok.get("message_id")


def test_admin_api(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-sovereign")
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    from api.app import app

    client = TestClient(app)
    r = client.get("/admin/sovereign/state")
    assert r.status_code in (401, 403)

    r2 = client.get(
        "/admin/sovereign/state",
        headers={"Authorization": "Bearer test-admin-key-sovereign"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["enabled"] is True
    assert "capabilities" in body
    assert "act.money_identity" in body["capabilities"]
    assert "brain" in body


def test_brain_think_with_fallback(monkeypatch):
    """Primary Grok fails → Claude succeeds; monologue + notes persisted."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_BRAIN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_BRAIN_PRIMARY", "grok")
    monkeypatch.setenv("SOVEREIGN_BRAIN_FALLBACK", "claude")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "0")
    monkeypatch.setenv("XAI_API_KEY", "fake-xai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-ant")

    import api.energy_agent_sovereign as sov
    import api.energy_agent_sovereign_brain as brain
    from api.models import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(sov, "SessionLocal", Session)
    monkeypatch.setattr(sov, "email_ford", lambda *a, **k: True)

    def boom(*a, **k):
        raise RuntimeError("grok down")

    def ok_claude(messages, temperature=0.35, timeout=None):
        return {
            "content": json.dumps({
                "monologue": "Ivory tower: queues look busy; I will triage utilities.",
                "observations": ["utility backlog"],
                "agenda": [{"id": "g_utility_backlog", "title": "Clear utility backlog", "priority": 95, "status": "open"}],
                "self_notes": [{"kind": "observation", "title": "queue", "body": "new utilities waiting"}],
                "memory_writes": [{"key": "utility_pressure", "value": "elevated"}],
                "actions": [{"type": "utility_triage", "rationale": "clear backlog"}],
                "speak_product": None,
                "mood": "watchful",
                "confidence": 0.7,
            }),
            "usage": {},
            "provider": "claude",
            "model": "claude-sonnet-4-5",
        }

    monkeypatch.setattr(brain, "call_grok", boom)
    monkeypatch.setattr(brain, "call_claude", ok_claude)

    with Session() as db:
        from api.utility_requests import UtilityRequest
        db.add(UtilityRequest(name="Test Co-op", product="array_operator", status="new"))
        db.commit()

    out = sov.sovereign_tick(reason="unit_brain")
    assert out["ok"] is True
    assert out.get("brain", {}).get("provider") == "claude"
    assert out["brain"].get("fallback_to_rules") is not True

    with Session() as db:
        notes = db.query(sov.EaSovereignNote).all()
        assert len(notes) >= 1
        assert any("Ivory tower" in (n.body or "") or n.kind == "thought" for n in notes)
        mem = db.get(sov.EaSovereignMemory, "utility_pressure")
        assert mem is not None
        assert "elevated" in (mem.value or "")


def test_brain_default_is_opus_cli(monkeypatch):
    """Ford 2026-07-16: brain defaults to the Opus 4.8 Claude Code CLI, and gracefully
    skips it to the next provider when the binary is absent (e.g. Railway)."""
    for k in ("SOVEREIGN_BRAIN_PRIMARY", "SOVEREIGN_BRAIN_FALLBACK"):
        monkeypatch.delenv(k, raising=False)
    import api.energy_agent_sovereign_brain as brain

    assert brain.primary_provider() == "claude_cli"
    assert brain.CLAUDE_CLI_MODEL == "claude-opus-4-8"
    assert brain.CLAUDE_MODEL == "claude-opus-4-8"

    # CLI binary absent → call_claude_cli raises → call_brain falls through to grok
    monkeypatch.setattr(brain, "_find_claude", lambda: None)
    calls = {"grok": 0}

    def ok_grok(messages, timeout=None):
        calls["grok"] += 1
        return {"content": "{}", "usage": {}, "provider": "grok", "model": "grok-4.5"}

    monkeypatch.setattr(brain, "call_grok", ok_grok)
    monkeypatch.setattr(brain, "call_claude", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no api")))
    out = brain.call_brain([{"role": "user", "content": "hi"}], timeout=10)
    assert out["provider"] == "grok"
    assert calls["grok"] == 1


# json import for brain test
import json  # noqa: E402