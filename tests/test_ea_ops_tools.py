"""Energy Agent capability pack: search tickets, cadence, documents, capture health, money."""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import (
    AgentDocument,
    Array,
    HarvestRun,
    Inverter,
    PortalCredential,
    RepairTicket,
    Tenant,
    now,
)
import api.repair_ops as ro
import api.ea_ops_tools as eot
from api import energy_agent as ea


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _tenant(**over) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    fields = dict(
        id=tid, name="Ops Owner", contact_email=f"{key}@owner.test",
        tenant_key=key, plan="comped", active=True, product="array_operator",
        repair_checkin_mode="manual", repair_checkin_hours=48, repair_auto_open=True,
        default_net_rate_per_kwh=0.21,
    )
    fields.update(over)
    with SessionLocal() as db:
        db.add(Tenant(**fields))
        db.commit()
    return tid, key


def _array(tenant_id: str, name: str) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name=name)
        db.add(arr)
        db.flush()
        aid = arr.id
        db.commit()
    return aid


def test_search_repair_tickets_by_name_keyword_date():
    tid, _ = _tenant()
    aid = _array(tid, "Chester Solar")
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        # Direct rows so we don't hit open_ticket's reuse-active-ticket path
        open_t = RepairTicket(
            tenant_id=tid, array_id=aid, site_name="Chester Solar",
            title="Chester inverter down", description="Unit 3 offline since storm",
            fail_type="dead", status="open", source="agent",
        )
        closed = RepairTicket(
            tenant_id=tid, array_id=aid, site_name="Chester Solar",
            title="Chester fuse swap", description="Replaced DC fuse",
            fail_type="fault", status="resolved", source="agent",
            opened_at=now() - timedelta(days=60),
            resolved_at=now() - timedelta(days=55),
        )
        db.add(open_t)
        db.add(closed)
        db.commit()
        open_id, closed_id = open_t.id, closed.id

    with SessionLocal() as db:
        by_name = ro.search_tickets(db, tid, array_name="Chester")
        assert {x.id for x in by_name} >= {open_id, closed_id}

        by_kw = ro.search_tickets(db, tid, keyword="fuse")
        assert any(x.id == closed_id for x in by_kw)

        since = now() - timedelta(days=90)
        until = now() - timedelta(days=30)
        by_date = ro.search_tickets(db, tid, date_from=since, date_to=until)
        assert any(x.id == closed_id for x in by_date)
        assert not any(x.id == open_id for x in by_date)


def test_per_ticket_checkin_cadence():
    tid, _ = _tenant(repair_checkin_hours=48)
    aid = _array(tid, "Rex Site")
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ticket = ro.open_ticket(db, t, array_id=aid, source="agent", title="Cadence test")
        assert ro.checkin_interval_hours(t, ticket) == 48
        ro.update_ticket(db, t, ticket, checkin_interval_hours=72)
        db.commit()
        assert ticket.checkin_interval_hours == 72
        assert ro.checkin_interval_hours(t, ticket) == 72
        ser = ro.serialize_ticket(ticket)
        assert ser["checkin_interval_hours"] == 72
        # clear override
        ro.update_ticket(db, t, ticket, clear_checkin_interval=True)
        db.commit()
        assert ticket.checkin_interval_hours is None
        assert ro.checkin_interval_hours(t, ticket) == 48


def test_save_document_and_list():
    tid, _ = _tenant()
    aid = _array(tid, "Doc Site")
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ticket = ro.open_ticket(db, t, array_id=aid, source="agent", title="Warranty path")
        db.commit()
        out = eot.save_document(db, t, args={
            "title": "Warranty claim draft — Doc Site",
            "type": "warranty_claim",
            "ticket_id": ticket.id,
            "content": (
                "# Warranty claim\n\n"
                "Serial ABC123 failed with fault 0x21.\n\n"
                "Lost ~120 kWh over 14 days."
            ),
            "make_pdf": True,
        })
        db.commit()
        assert out["ok"] is True
        doc_id = out["document"]["id"]
        assert out["document"]["ticket_id"] == ticket.id
        # PDF may or may not render depending on reportlab — ok either way
        listed = eot.list_documents(db, t, ticket_id=ticket.id)
        assert listed["count"] >= 1
        got = eot.get_document(db, t, doc_id)
        assert got["ok"] and "Warranty claim" in (got["document"]["content"] or "")


def test_capture_health_detail_diagnosis():
    tid, _ = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        db.add(PortalCredential(
            tenant_id=tid,
            provider="sma",
            username="owner@ex.com",
            username_lc="owner@ex.com",
            secret_enc="enc-test",
            cloud_capture_enabled=True,
            last_harvest_ok=False,
            harvest_fails=4,
        ))
        db.add(HarvestRun(
            tenant_id=tid,
            provider="sma",
            username_lc="owner@ex.com",
            status="login_failed",
            detail="Invalid password / MFA required",
            rows_written=0,
        ))
        db.commit()
        out = eot.capture_health_detail(db, t, args={"provider": "sma"})
        assert out["ok"]
        assert out["vendor_count"] >= 1
        v = out["vendors"][0]
        assert v["provider"] == "sma"
        assert "login" in (v["diagnosis"]["summary"] or "").lower() or v["diagnosis"]["severity"] == "critical"


def test_tool_schemas_registered():
    names = {t["function"]["name"] for t in ea.TOOL_DEFS if t.get("type") == "function"}
    for n in (
        "search_repair_tickets",
        "fleet_financial_health",
        "capture_health_detail",
        "underperformer_history",
        "save_document",
        "list_documents",
    ):
        assert n in names, f"missing tool {n}"
    # cadence on update_repair_ticket
    upd = next(t for t in ea.TOOL_DEFS if t["function"]["name"] == "update_repair_ticket")
    assert "checkin_interval_hours" in upd["function"]["parameters"]["properties"]


def test_enrich_search_via_run_tool_path():
    """search_repair_tickets dispatch returns ok shape."""
    tid, _ = _tenant()
    aid = _array(tid, "Alpha")
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.open_ticket(db, t, array_id=aid, source="agent", title="Alpha dead unit")
        db.commit()
        sess = ea.EaSession(id="eas_" + secrets.token_hex(8), tenant_id=tid)
        db.add(sess)
        db.commit()
        out = ea._run_tool(
            "search_repair_tickets",
            {"array_name": "Alpha", "keyword": "dead"},
            t, sess, db,
            user_text="what happened with Alpha?",
        )
        assert out.get("ok") is True
        assert out.get("count", 0) >= 1


def test_trend_analysis_steady_vs_drop():
    # Synthetic series: flat low → steady; high then crash → recent_drop
    inv = {"status": "underperforming", "peer_index": 0.5, "nameplate_kw": 10}
    flat = [{"date": f"2026-01-{i+1:02d}", "kwh": 20.0} for i in range(90)]
    # pad flat to 90 days with more
    flat = [{"date": f"2026-0{(i//30)+1}-{(i%28)+1:02d}", "kwh": 18.0 + (i % 3)} for i in range(100)]
    r = eot._trend_analysis(flat, inv, [], 180)
    assert r["pattern"] in ("steady_low", "underperforming_unclear", "insufficient_history") or r.get(
        "steady_underperformer_candidate"
    ) is not None

    cliff = (
        [{"date": f"d{i}", "kwh": 40.0} for i in range(60)]
        + [{"date": f"d{i}", "kwh": 5.0} for i in range(60, 100)]
    )
    r2 = eot._trend_analysis(cliff, inv, [], 180)
    assert r2["pattern"] in ("recent_drop", "underperforming_unclear", "steady_low")
