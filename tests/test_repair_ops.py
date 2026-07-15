"""O&M healing — service contacts, repair tickets, check-ins.

Covers api/repair_ops.py:
  - contact upsert + default uniqueness + array assignment
  - open ticket + draft check-in
  - reconcile opens dead/fault when contact known; skips without contact
  - reconcile clears/resolves recovered inverters
  - send_checkin logs RepairCheckIn + bumps status
  - process_due respects checkin mode
  - REST list/create paths via dual auth
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api.repair_ops as ro
from api.db import SessionLocal, init_db
from api.models import (
    Array,
    ArrayServiceAssignment,
    Inverter,
    RepairCheckIn,
    RepairTicket,
    ServiceContact,
    Tenant,
    now,
)


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


def _inv(tid, aid, *, name="Inv 1", np=10.0) -> tuple[int, str]:
    with SessionLocal() as db:
        iv = Inverter(
            tenant_id=tid, array_id=aid, vendor="solaredge",
            serial="SN-" + secrets.token_hex(3), position=0,
            name=name, model=f"SE{np}K", nameplate_kw=np,
        )
        db.add(iv)
        db.flush()
        rid, sn = iv.id, iv.serial
        db.commit()
    return rid, sn


def _tree(array_id, site, invs):
    return {
        "columns": [{
            "array_id": array_id,
            "array_name": site,
            "inverters": invs,
            "alert": {"status": invs[0]["status"] if invs else "ok"},
        }],
        "summary": {},
    }


def _inv_row(inv_id, status, *, sn=None, name="Inv 1"):
    return {
        "inverter_id": inv_id, "sn": sn or f"SN-{inv_id}", "name": name,
        "vendor": "solaredge", "status": status, "diagnosis": f"{status} unit",
        "window_kwh": 0.0 if status in ("dead", "fault") else 100.0,
        "nameplate_kw": 10.0, "peer_index": 0.0 if status == "dead" else 1.0,
    }


# ── contacts ──────────────────────────────────────────────────────────────────

def test_upsert_contact_and_default_unique():
    tid, _ = _tenant()
    with SessionLocal() as db:
        a = ro.upsert_contact(
            db, tid, name="Alex Tech", email="alex@om.test",
            role="om", is_default=True, company="Green Fix LLC",
        )
        b = ro.upsert_contact(
            db, tid, name="Blake Electric", email="blake@om.test",
            role="electrician", is_default=True,
        )
        db.commit()
        db.refresh(a)
        db.refresh(b)
        assert b.is_default is True
        assert a.is_default is False
        listed = ro.list_contacts(db, tid)
        assert len(listed) == 2
        assert listed[0].is_default is True  # default first


def test_assign_array_contact():
    tid, _ = _tenant()
    aid = _array(tid, "Barn Roof")
    with SessionLocal() as db:
        c = ro.upsert_contact(db, tid, name="Casey", email="c@om.test", role="installer")
        db.flush()
        row = ro.assign_array_contact(db, tid, aid, c.id, kind="primary")
        db.commit()
        assert row.array_id == aid
        resolved = ro.resolve_contact_for_array(db, tid, aid)
        assert resolved is not None
        assert resolved.id == c.id


# ── tickets + reconcile ───────────────────────────────────────────────────────

def test_open_ticket_drafts_checkin():
    tid, _ = _tenant()
    aid = _array(tid, "Hilltop")
    iid, sn = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        c = ro.upsert_contact(db, tid, name="Dana", email="dana@om.test", is_default=True)
        db.flush()
        ticket = ro.open_ticket(
            db, t, array_id=aid, inverter_id=iid, fail_type="dead",
            source="agent",
        )
        db.commit()
        assert ticket.contact_id == c.id
        assert ticket.status == "open"
        draft = ticket.draft_checkin or {}
        assert draft.get("to") == "dana@om.test"
        assert "Hilltop" in (draft.get("subject") or "")
        assert sn in (draft.get("body") or "") or "Inv" in (draft.get("body") or "")


def test_reconcile_opens_only_with_contact():
    tid, _ = _tenant()
    aid = _array(tid, "No Contact Site")
    iid, sn = _inv(tid, aid)
    tree = _tree(aid, "No Contact Site", [_inv_row(iid, "dead", sn=sn)])
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        out = ro.reconcile(db, t, tree=tree)
        assert out["opened"] == 0  # no contact

        ro.upsert_contact(db, tid, name="Eve", email="eve@om.test", is_default=True)
        db.commit()
        t = db.get(Tenant, tid)
        out2 = ro.reconcile(db, t, tree=tree)
        assert out2["opened"] == 1
        tickets = ro.list_tickets(db, tid, active_only=True)
        assert len(tickets) == 1
        assert tickets[0].fail_type == "dead"
        assert tickets[0].serial == sn

        # idempotent
        out3 = ro.reconcile(db, t, tree=tree)
        assert out3["opened"] == 0


def test_reconcile_clears_recovered_before_contact():
    tid, _ = _tenant()
    aid = _array(tid, "Recover Site")
    iid, sn = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(db, tid, name="Fay", email="fay@om.test", is_default=True)
        db.commit()
        t = db.get(Tenant, tid)
        dead = _tree(aid, "Recover Site", [_inv_row(iid, "fault", sn=sn)])
        ro.reconcile(db, t, tree=dead)
        tickets = ro.list_tickets(db, tid, active_only=True)
        assert len(tickets) == 1

        ok = _tree(aid, "Recover Site", [_inv_row(iid, "ok", sn=sn)])
        out = ro.reconcile(db, t, tree=ok)
        assert out["closed"] >= 1
        tickets2 = ro.list_tickets(db, tid, active_only=True)
        assert len(tickets2) == 0


def test_send_checkin_and_note_status_bump():
    tid, _ = _tenant()
    aid = _array(tid, "Send Site")
    iid, sn = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(db, tid, name="Gus", email="gus@om.test", is_default=True)
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        db.commit()
        tid_ticket = ticket.id

    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ) as mock_send:
        t = db.get(Tenant, tid)
        ticket = db.get(RepairTicket, tid_ticket)
        row = ro.send_checkin(db, t, ticket, via="agent")
        db.commit()
        assert row.sent_ok is True
        assert mock_send.called
        db.refresh(ticket)
        assert ticket.status == "waiting_reply"
        assert ticket.checkin_count == 1

        ro.log_inbound_note(db, t, ticket, "Scheduled visit Thursday morning")
        db.commit()
        db.refresh(ticket)
        assert ticket.status == "scheduled"

        ro.log_inbound_note(db, t, ticket, "Unit replaced and back online — fixed")
        db.commit()
        db.refresh(ticket)
        assert ticket.status == "resolved"


def test_process_due_auto_mode():
    tid, _ = _tenant(repair_checkin_mode="auto", repair_checkin_hours=24)
    aid = _array(tid, "Auto Site")
    iid, sn = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(db, tid, name="Hank", email="hank@om.test", is_default=True)
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="fault")
        # due now
        ticket.next_checkin_at = now() - timedelta(minutes=5)
        db.commit()
        tid_ticket = ticket.id

    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ):
        t = db.get(Tenant, tid)
        n = ro.process_due(db, t)
        assert n == 1
        ticket = db.get(RepairTicket, tid_ticket)
        assert ticket.checkin_count == 1


def test_manual_mode_does_not_auto_send():
    tid, _ = _tenant(repair_checkin_mode="manual")
    aid = _array(tid, "Manual Site")
    iid, _ = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(db, tid, name="Ivy", email="ivy@om.test", is_default=True)
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        ticket.next_checkin_at = now() - timedelta(hours=1)
        db.commit()
    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ) as mock_send:
        t = db.get(Tenant, tid)
        n = ro.process_due(db, t)
        assert n == 0
        assert not mock_send.called


# ── REST ──────────────────────────────────────────────────────────────────────

def test_rest_contacts_and_ops_overview():
    from api.app import app
    tid, key = _tenant()
    aid = _array(tid, "REST Site")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {key}"}

    r = client.post(
        "/v1/array-owners/ops/contacts",
        headers=headers,
        json={
            "name": "Jules", "email": "jules@om.test",
            "role": "om", "is_default": True, "company": "Jules O&M",
        },
    )
    assert r.status_code == 200, r.text
    cid = r.json()["contact"]["id"]

    r2 = client.post(
        "/v1/array-owners/ops/assign",
        headers=headers,
        json={"array_id": aid, "contact_id": cid, "kind": "primary"},
    )
    assert r2.status_code == 200, r2.text

    r3 = client.get("/v1/array-owners/ops/contacts", headers=headers)
    assert r3.status_code == 200
    body = r3.json()
    assert body["count"] if "count" in body else len(body["contacts"]) >= 1
    assert any(c["name"] == "Jules" for c in body["contacts"])

    r4 = client.post(
        "/v1/array-owners/ops/tickets",
        headers=headers,
        json={"array_id": aid, "fail_type": "dead", "title": "REST dead inverter"},
    )
    assert r4.status_code == 200, r4.text
    ticket = r4.json()["ticket"]
    assert ticket["contact_id"] == cid
    assert ticket["draft_checkin"]["to"] == "jules@om.test"

    with patch.object(ro.notify, "send_repair_checkin_email", return_value=True):
        r5 = client.post(
            f"/v1/array-owners/ops/tickets/{ticket['id']}/checkin",
            headers=headers,
            json={},
        )
    assert r5.status_code == 200, r5.text
    assert r5.json()["checkin"]["sent_ok"] is True

    r6 = client.get("/v1/array-owners/ops?reconcile_first=0", headers=headers)
    assert r6.status_code == 200, r6.text
    assert r6.json()["summary"]["open"] >= 1


def test_energy_agent_tools_list_and_upsert():
    """Direct tool handlers (no LLM)."""
    from api.energy_agent import _run_tool, EaSession
    from api.models import Base
    tid, key = _tenant()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        # minimal fake session row not required for these tools
        class _S:
            id = "sess_test"
            tenant_id = tid
        out = _run_tool(
            "upsert_service_contact",
            {
                "name": "Kim Ops", "email": "kim@om.test",
                "role": "om", "is_default": True, "needs_confirm": False,
            },
            t, _S(), db, user_text="add Kim Ops kim@om.test as my default O&M contact",
        )
        assert out.get("ok") is True, out
        assert out["contact"]["email"] == "kim@om.test"

        overview = _run_tool(
            "repair_ops_overview",
            {"reconcile": False},
            t, _S(), db,
        )
        assert overview.get("ok") is True
        assert any(c["name"] == "Kim Ops" for c in overview.get("contacts") or [])


def test_phone_note_and_sms_uri():
    tid, _ = _tenant()
    aid = _array(tid, "Phone Site")
    iid, sn = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(
            db, tid, name="Lee", email="lee@om.test", phone="+18025550199",
            is_default=True,
        )
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        db.commit()
        tid_ticket = ticket.id

    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ticket = db.get(RepairTicket, tid_ticket)
        row = ro.log_phone_note(db, t, ticket, "Said parts on order for Friday")
        db.commit()
        assert row.channel == "phone_note"
        db.refresh(ticket)
        assert ticket.status == "in_progress"

    with SessionLocal() as db, patch.object(ro.notify, "send_repair_sms", return_value=False):
        t = db.get(Tenant, tid)
        ticket = db.get(RepairTicket, tid_ticket)
        out = ro.send_or_log_sms(db, t, ticket, "Any update?", via="test")
        db.commit()
        assert out["ok"] is True
        assert out["sms_uri"].startswith("sms:")
        assert out["sent_via_twilio"] is False
        assert f"[AO-TICKET-{tid_ticket}]" in (out["checkin"]["body"] or "")


def test_inbound_email_parse_and_claim_link():
    tid, _ = _tenant()
    aid = _array(tid, "Inbound Site")
    iid, sn = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        c = ro.upsert_contact(
            db, tid, name="Mo", email="mo@om.test", is_default=True,
        )
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="fault")
        db.commit()
        tid_ticket = ticket.id
        contact_id = c.id

    # Marker in subject
    with SessionLocal() as db:
        out = ro.ingest_inbound_email(
            db,
            from_email="mo@om.test",
            subject=f"Re: status [AO-TICKET-{tid_ticket}]",
            body="Scheduled visit Thursday morning.",
        )
        assert out["ok"] is True
        assert out["ticket_id"] == tid_ticket
        assert out["status"] == "scheduled"

    # Link warranty claim
    from api.models import WarrantyClaim
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        claim = WarrantyClaim(
            tenant_id=tid, array_id=aid, inverter_id=iid,
            serial=sn, inv_name="Inv 1", vendor="solaredge",
            site_name="Inbound Site", fail_type="fault",
            evidence={}, draft={}, stage="ready",
        )
        db.add(claim)
        db.flush()
        ticket = db.get(RepairTicket, tid_ticket)
        ro.link_warranty_claim(db, t, ticket, claim.id)
        db.commit()
        claim_id = claim.id

    with SessionLocal() as db:
        ticket = db.get(RepairTicket, tid_ticket)
        assert ticket.warranty_claim_id == claim_id
        summary = ro.claim_summary_for_ticket(db, ticket)
        assert summary and summary["id"] == claim_id


def test_extract_ticket_id():
    assert ro.extract_ticket_id_from_text("hello [AO-TICKET-42] world") == 42
    assert ro.extract_ticket_id_from_text("Re: Ticket #99 status") == 99
    assert ro.extract_ticket_id_from_text("no marker here") is None
