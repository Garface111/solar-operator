"""Temporary + permanent email copy overrides (Energy Agent)."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from api.db import SessionLocal
from api.models import EmailCopyOverride, Tenant, now
from api import email_copy_overrides as eco


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Copy Co", contact_email=f"{tid}@t.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="standard", active=True, product="array_operator",
            offtaker_email_body_template="<p>PERMANENT offtaker letter</p>",
            offtaker_email_subject_template="Permanent subject {{offtaker_name}}",
            email_body_template="<p>PERMANENT gen-report letter</p>",
            email_subject_template="Gen report for {{client_name}}",
        ))
        db.commit()
    return tid


def test_permanent_and_one_shot_append():
    tid = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        r = eco.resolve_templates(db, t, "offtaker")
        assert r["source"] == "permanent"
        assert "PERMANENT offtaker" in (r["body_template"] or "")
        assert r["override_id"] is None

        ov = eco.schedule_override(
            db, tid, "offtaker",
            body_append="Happy New Year — rates update next month.",
            max_sends=1,
            reason="January notice",
        )
        db.commit()
        assert ov.id

        r2 = eco.resolve_templates(db, t, "offtaker")
        assert r2["source"] == "override"
        assert r2["override_id"] == ov.id
        assert "Happy New Year" in (r2["body_append"] or "")
        body = eco.apply_body_append(r2["body_template"], r2["body_append"])
        assert "PERMANENT offtaker" in body
        assert "Happy New Year" in body

        # After one send, override exhausts
        eco.record_send(db, ov.id)
        db.commit()
        ov2 = db.get(EmailCopyOverride, ov.id)
        assert ov2.status == "exhausted"
        assert ov2.sends_used == 1

        r3 = eco.resolve_templates(db, t, "offtaker")
        assert r3["source"] == "permanent"
        assert r3["override_id"] is None


def test_scheduled_future_start():
    tid = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        future = datetime.utcnow() + timedelta(days=7)
        ov = eco.schedule_override(
            db, tid, "generation_report",
            body_template="<p>AUGUST ONLY letter</p>",
            starts_at=future,
            ends_at=future + timedelta(days=30),
            max_sends=None,
        )
        db.commit()
        r = eco.resolve_templates(db, t, "generation_report")
        # Not active yet
        assert r["source"] == "permanent"
        assert "PERMANENT gen-report" in (r["body_template"] or "")

        # Pretend we're in the window
        ov2 = db.get(EmailCopyOverride, ov.id)
        ov2.starts_at = datetime.utcnow() - timedelta(hours=1)
        db.commit()
        r2 = eco.resolve_templates(db, t, "generation_report")
        assert r2["source"] == "override"
        assert "AUGUST ONLY" in (r2["body_template"] or "")


def test_cancel_override():
    tid = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ov = eco.schedule_override(
            db, tid, "offtaker", body_append="temp", max_sends=5,
        )
        db.commit()
        out = eco.cancel_override(db, tid, ov.id)
        db.commit()
        assert out["status"] == "cancelled"
        r = eco.resolve_templates(db, t, "offtaker")
        assert r["source"] == "permanent"


def test_set_permanent():
    tid = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        eco.set_permanent(
            db, t, "offtaker",
            body_template="<p>NEW MASTER</p>",
            subject_template="New subj",
        )
        db.commit()
        db.refresh(t)
        assert t.offtaker_email_body_template == "<p>NEW MASTER</p>"
        eco.set_permanent(db, t, "offtaker", clear_body=True, clear_subject=True)
        db.commit()
        db.refresh(t)
        assert t.offtaker_email_body_template is None


def test_energy_agent_tools(client):
    tid = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        key = t.tenant_key

    # Mint EA session via tool path is heavy — call tool fns directly
    from api.energy_agent import _email_copy_tool
    from api.models import Tenant as T

    with SessionLocal() as db:
        t = db.get(T, tid)
        out = _email_copy_tool("get_email_copy", db, t, {"channel": "offtaker"})
        assert out["ok"] is True
        assert out["permanent"]["body_template"]

        out2 = _email_copy_tool("schedule_email_copy", db, t, {
            "channel": "offtaker",
            "body_append": "Holiday line only",
            "max_sends": 1,
            "needs_confirm": False,
            "reason": "test",
        })
        assert out2["ok"] is True
        oid = out2["override"]["id"]

        out3 = _email_copy_tool("list_email_copy_overrides", db, t, {
            "channel": "offtaker",
        })
        assert out3["count"] >= 1

        out4 = _email_copy_tool("cancel_email_copy_override", db, t, {
            "override_id": oid,
            "needs_confirm": False,
        })
        assert out4["ok"] is True
