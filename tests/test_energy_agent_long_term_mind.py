"""Phase E — long-term mind: world profile, wake, proactive insight."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.energy_agent_mind import (
    EaEvent,
    EaPlan,
    EaTask,
    EaWorldState,
    MIN_IMPORTANCE_TO_SPEAK,
    _default_world,
    _proactive_insight_worker,
    _world_get,
    _world_patch,
    score_importance,
    wake_mind,
)
from api.models import Tenant


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    EaWorldState.__table__.create(engine)
    EaPlan.__table__.create(engine)
    EaTask.__table__.create(engine)
    EaEvent.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_default_world_has_profile():
    w = _default_world()
    # Owner email defaults OFF — hard allowlist required at send time
    assert w["profile"]["email_insights"] is False
    assert w["profile"]["email_ux_approvals"] is False
    assert w["profile"]["voice_pref"] == "one_mind"
    assert w["insights"] == []
    assert w["pending_approvals"] == []


def test_world_merge_email_opt_in_only_when_explicit(db):
    tid = "ten_lt1"
    w = _world_get(db, tid)
    assert w["profile"]["email_insights"] is False
    # Explicit True is preserved (allowlist still gates send)
    _world_patch(db, tid, {"profile": {"auto_approve_ux": True, "email_insights": True}})
    w2 = _world_get(db, tid)
    assert w2["profile"]["auto_approve_ux"] is True
    assert w2["profile"]["email_insights"] is True
    # Missing / non-True does not auto-enable
    _world_patch(db, tid, {"profile": {"email_insights": False}})
    assert _world_get(db, tid)["profile"]["email_insights"] is False


def test_owner_email_allowlist_and_defaults_off():
    from api.energy_agent_mind import _owner_email_allowed, _email_owner_and_ford

    assert _owner_email_allowed("ford.genereaux@gmail.com") is True
    assert _owner_email_allowed("ford.genereaux+ao@gmail.com") is True
    assert _owner_email_allowed("bruce.genereaux@gmail.com") is False
    assert _owner_email_allowed("random@customer.com") is False
    assert _owner_email_allowed(None) is False

    tenant = MagicMock()
    tenant.id = "ten_x"
    tenant.contact_email = "random@customer.com"
    with patch("api.notify.send_internal_alert") as sia, \
         patch("api.notify._send_via_resend") as resend:
        sia.return_value = True
        resend.return_value = True
        # owner=False by default path — nothing sent when both false
        out0 = _email_owner_and_ford(tenant, subject="t", body="b")
        assert out0.get("skipped") is True
        resend.assert_not_called()
        # Even if caller asks owner=True, non-allowlisted blocked
        out = _email_owner_and_ford(
            tenant, subject="test", body="body", owner=True, ford=False,
        )
    assert out["owner"] is False
    assert out.get("owner_blocked") is True
    resend.assert_not_called()


def test_opt_in_phrase_sets_profile(db):
    from api.energy_agent_mind import classify_and_plan
    tid = "ten_opt"
    with patch("api.energy_agent._mem_set"), patch("api.energy_agent._mem_get", return_value=[]):
        classify_and_plan(db, tid, "s1", "Please email me updates about the fleet.")
    w = _world_get(db, tid)
    assert w["profile"]["email_insights"] is True
    assert w["profile"]["email_ux_approvals"] is True
    with patch("api.energy_agent._mem_set"), patch("api.energy_agent._mem_get", return_value=[]):
        classify_and_plan(db, tid, "s1", "Stop email updates please.")
    w2 = _world_get(db, tid)
    assert w2["profile"]["email_insights"] is False


def test_score_proactive_insight():
    n = score_importance(
        "proactive_insight",
        {"insight": {"importance": 80}, "emailed": {"owner": False}},
        "Fleet needs attention at Cover",
    )
    assert n >= MIN_IMPORTANCE_TO_SPEAK
    assert n <= 100
    soft = score_importance(
        "proactive_insight",
        {"insight": {"importance": 80}, "emailed": {"owner": True}},
        "Fleet needs attention",
    )
    assert soft < n


def test_wake_records_reason_and_emits(db):
    tid = "ten_lt2"
    with patch("api.energy_agent_mind.drain_tasks", return_value=0):
        out = wake_mind(db, tid, "fleet_attention", payload={"n": 2})
    assert out["ok"] is True
    assert out["reason"] == "fleet_attention"
    w = _world_get(db, tid)
    assert w.get("last_wake_reason") == "fleet_attention"
    wakes = db.query(EaEvent).filter(EaEvent.kind == "mind_wake").all()
    assert len(wakes) >= 1
    tasks = db.query(EaTask).filter(EaTask.tenant_id == tid).all()
    kinds = {t.kind for t in tasks}
    assert "fleet_pulse" in kinds or "proactive_insight" in kinds


def test_proactive_insight_worker_quiet_fleet(db):
    tid = "ten_lt3"
    fake_tenant = MagicMock(spec=Tenant)
    fake_tenant.id = tid
    fake_tenant.contact_email = "owner@example.com"

    real_get = db.get

    def smart_get(model, ident):
        if model is Tenant or getattr(model, "__name__", "") == "Tenant":
            return fake_tenant
        return real_get(model, ident)

    with patch.object(db, "get", side_effect=smart_get), \
         patch(
             "api.energy_agent._tenant_census_tool",
             return_value={"totals": {"arrays": 3, "inverters": 12}},
         ), \
         patch(
             "api.energy_agent._investigate_attention_tool",
             return_value={"count": 0, "brief": "all clear", "problems": []},
         ), \
         patch(
             "api.energy_agent_mind._email_owner_and_ford",
             return_value={"owner": False, "ford": False},
         ), \
         patch(
             "api.energy_agent_mind._count_recent_ux_friction",
             return_value=0,
         ):
        r = _proactive_insight_worker(db, tid, {"reason": "test"})

    assert r.get("ok") is True
    assert r["insight"]["attention_count"] == 0
    text = (r["insight"]["headline"] + " " + r["insight"]["detail"]).lower()
    assert "clear" in text or "nothing needs" in text
    w = _world_get(db, tid)
    assert w.get("insights")
    assert w.get("last_proactive_at")


def test_proactive_insight_attention_first_notice_speaks(db):
    tid = "ten_lt4"
    fake_tenant = MagicMock(spec=Tenant)
    fake_tenant.id = tid
    fake_tenant.contact_email = "owner@example.com"
    real_get = db.get

    def smart_get(model, ident):
        if model is Tenant or getattr(model, "__name__", "") == "Tenant":
            return fake_tenant
        return real_get(model, ident)

    with patch.object(db, "get", side_effect=smart_get), \
         patch(
             "api.energy_agent._tenant_census_tool",
             return_value={"totals": {"arrays": 4, "inverters": 39}},
         ), \
         patch(
             "api.energy_agent._investigate_attention_tool",
             return_value={
                 "count": 2,
                 "brief": "2 flagged",
                 "problems": [
                     {
                         "name": "Cover Rooftop",
                         "why": "underperforming vs peers",
                         "next_step": "See diagnosis",
                     }
                 ],
             },
         ), \
         patch(
             "api.energy_agent_mind._email_owner_and_ford",
             return_value={"owner": False, "ford": False},
         ), \
         patch(
             "api.energy_agent_mind._count_recent_ux_friction",
             return_value=0,
         ):
        r = _proactive_insight_worker(db, tid, {"reason": "alert"})

    assert r["ok"] is True
    assert r["insight"]["attention_count"] == 2
    assert "Cover" in r["insight"]["headline"] or "Cover" in r["insight"]["detail"]
    # First notice may speak once
    assert r.get("speak") or r.get("silent") is False


def test_proactive_insight_same_story_is_silent(db):
    tid = "ten_lt5"
    fake_tenant = MagicMock(spec=Tenant)
    fake_tenant.id = tid
    fake_tenant.contact_email = "owner@example.com"
    real_get = db.get

    def smart_get(model, ident):
        if model is Tenant or getattr(model, "__name__", "") == "Tenant":
            return fake_tenant
        return real_get(model, ident)

    att = {
        "count": 2,
        "brief": "2 flagged",
        "problems": [
            {
                "name": "Tannery Brook",
                "why": "underperforming",
                "next_step": "Check peers",
            }
        ],
    }
    with patch.object(db, "get", side_effect=smart_get), \
         patch("api.energy_agent._tenant_census_tool",
               return_value={"totals": {"arrays": 8, "inverters": 63}}), \
         patch("api.energy_agent._investigate_attention_tool", return_value=att), \
         patch("api.energy_agent_mind._email_owner_and_ford",
               return_value={"owner": False, "ford": False}), \
         patch("api.energy_agent_mind._count_recent_ux_friction", return_value=0):
        r1 = _proactive_insight_worker(db, tid, {"reason": "first"})
        r2 = _proactive_insight_worker(db, tid, {"reason": "again"})

    assert r1["ok"] and r2["ok"]
    # Second run same fleet story should not speak again
    assert r2.get("duplicate") is True or r2.get("silent") is True or not r2.get("speak")