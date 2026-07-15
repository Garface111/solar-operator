"""Sovereign full ops authority — features, utilities, escalations, deploy, memory, jobs."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_OPS_AUTHORITY", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_CODE_LIVE", "0")  # jobs drain denied but stage ok
    monkeypatch.setenv("SOVEREIGN_CODE_DEPLOY", "0")

    from api.models import Base
    import api.energy_agent_sovereign as sov  # noqa: F401
    import api.feature_suggestions as fs_mod
    import api.utility_requests as ur_mod
    import api.ford_escalations as esc_mod
    import api.energy_agent_sovereign_ops as ops

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        yield db, ops, fs_mod, ur_mod, esc_mod, sov


def test_ops_enabled_default_on(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_OPS_AUTHORITY", "1")
    from api.energy_agent_sovereign_ops import ops_enabled
    assert ops_enabled() is True


def test_feature_triage_assign_ship(db_session):
    db, ops, fs_mod, ur_mod, esc_mod, sov = db_session
    fs = fs_mod.FeatureSuggestion(
        text="Add dark mode toggle to reports",
        email="owner@example.com",
        status="new",
    )
    db.add(fs)
    db.commit()
    fid = fs.id

    tri = ops.triage_feature_queue(db, limit=10)
    assert tri["ok"] and tri["triaged"] == 1
    db.refresh(fs)
    assert fs.status == "reviewed"

    assigned = ops.assign_feature(db, fid, assignee="sovereign", priority_note="P0 UX")
    assert assigned["ok"] and assigned["status"] == "building"

    # Put another in reviewed and batch-promote
    fs2 = fs_mod.FeatureSuggestion(text="Export CSV from fleet table", status="reviewed")
    db.add(fs2)
    db.commit()
    batch = ops.ship_reviewed_features(db, limit=5, also_code_hire=True)
    assert batch["ok"] and batch["count"] >= 1
    db.refresh(fs2)
    assert fs2.status == "building"
    assert batch["items"][0].get("code_job")

    shipped = ops.mark_feature_shipped(db, fs2.id, note="live on prod")
    assert shipped["ok"] and shipped["status"] == "shipped"


def test_utility_advance_requires_evidence_for_added(db_session):
    db, ops, fs_mod, ur_mod, esc_mod, sov = db_session
    r = ur_mod.UtilityRequest(
        name="Green Mountain Power",
        state="VT",
        url="https://example.com",
        status="reviewed",
        email="owner@example.com",
    )
    db.add(r)
    db.commit()

    adv = ops.advance_utility_queue(db, limit=3)
    assert adv["ok"] and adv["advanced"] == 1
    db.refresh(r)
    assert r.status == "researching"
    assert adv["items"][0].get("job")

    denied = ops.mark_utility_added(db, r.id, evidence="")
    assert denied.get("ok") is False
    ok = ops.mark_utility_added(db, r.id, evidence="SmartHub registry entry live + HAR")
    assert ok["ok"] and ok["status"] == "added"


def test_escalation_auto_resolve_and_blocklist(db_session):
    db, ops, fs_mod, ur_mod, esc_mod, sov = db_session
    e1 = esc_mod.EaEscalation(
        id="esc_test_1",
        tenant_id="ten_test",
        status="needs_ford",
        kind="bug",
        priority="high",
        summary="Reports chart broken on mobile",
        proposed_plan="Fix CSS overflow on report chart",
    )
    e2 = esc_mod.EaEscalation(
        id="esc_blocked",
        tenant_id="ten_test",
        status="needs_ford",
        kind="policy",
        priority="high",
        summary="Ford must decide brand color",
    )
    db.add_all([e1, e2])
    db.commit()

    # Block second
    sov.memory_set(db, "escalation_blocklist", json.dumps(["esc_blocked"]), source="ford")

    res = ops.auto_resolve_needs_ford(db, limit=10)
    assert res["ok"]
    db.refresh(e1)
    db.refresh(e2)
    assert e1.status == "done"
    # blocked one should not close
    blocked = ops.resolve_escalation(db, "esc_blocked", status="done", note="try close")
    assert blocked.get("ok") is False
    assert "blocked" in (blocked.get("denied_reason") or "").lower()
    assert e2.status == "needs_ford"


def test_memory_agenda_deploy_stage(db_session):
    db, ops, fs_mod, ur_mod, esc_mod, sov = db_session
    mem = ops.own_memory_write(db, "succession_gap", "still need Stripe identity")
    assert mem["ok"]
    all_m = {m["key"]: m["value"] for m in sov.memory_get_all(db)}
    assert "succession_gap" in all_m

    agenda = ops.own_agenda(db, [
        {"id": "g_ops", "title": "Clear feature_reviewed queue", "priority": 95, "status": "open"},
        {"id": "g_util", "title": "Advance utility adapters", "priority": 80, "status": "open"},
    ])
    assert agenda["ok"] and agenda["updated"] >= 1

    rep = ops.reprioritize_goals(db, [
        {"id": "g_ops", "title": "Clear feature_reviewed queue", "priority": 99},
    ])
    assert rep["ok"]

    dep = ops.stage_deploy(db, repo="array-operator", reason="ops authority test", execute_now=False)
    assert dep["ok"] and dep["staged"]
    assert dep.get("code_job")
    all_m = {m["key"]: m["value"] for m in sov.memory_get_all(db)}
    assert "deploy_stage" in all_m


def test_autonomous_sweep_and_summary(db_session):
    db, ops, fs_mod, ur_mod, esc_mod, sov = db_session
    db.add(fs_mod.FeatureSuggestion(text="A", status="new"))
    db.add(fs_mod.FeatureSuggestion(text="B", status="reviewed"))
    db.add(ur_mod.UtilityRequest(name="CoopX", status="new", state="MA"))
    db.add(esc_mod.EaEscalation(
        id="esc_sweep",
        tenant_id="ten_test",
        status="needs_ford",
        kind="ux",
        priority="med",
        summary="UI glitch on tickets panel",
    ))
    db.commit()

    summary = ops.ops_summary(db)
    assert summary["ops_authority"] is True
    assert summary["features"]["new"] >= 1
    assert summary["features"]["reviewed"] >= 1

    sweep = ops.autonomous_ops_sweep(db)
    assert sweep["ok"]
    assert (sweep.get("triage") or {}).get("triaged", 0) >= 1
    assert (sweep.get("features") or {}).get("count", 0) >= 1
    assert (sweep.get("utilities") or {}).get("advanced", 0) >= 1
    assert (sweep.get("escalations") or {}).get("resolved", 0) >= 1

    all_m = {m["key"]: m["value"] for m in sov.memory_get_all(db)}
    assert "ops_last_sweep" in all_m
    assert "ops_authority" in all_m


def test_execute_brain_actions_ops_types(db_session):
    db, ops, fs_mod, ur_mod, esc_mod, sov = db_session
    db.add(fs_mod.FeatureSuggestion(text="Brain path feature", status="reviewed"))
    db.commit()

    out = sov.execute_brain_actions(
        db,
        [
            {"type": "feature_ship_batch", "limit": 3},
            {"type": "memory_set", "key": "brain_ops", "value": "yes"},
            {"type": "agenda", "agenda": [{"id": "g_brain", "title": "Brain agenda", "priority": 70}]},
            {"type": "deploy_stage", "repo": "both", "text": "from brain"},
        ],
        tick_id="tick_test",
    )
    kinds = [o["kind"] for o in out]
    assert "feature_ship_batch" in kinds
    assert "memory_set" in kinds
    assert "agenda" in kinds
    assert "deploy_stage" in kinds
    assert all(o.get("result", {}).get("ok") is not False or o["kind"] == "wait" for o in out if o["kind"] != "wait")
