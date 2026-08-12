"""A retired device must never keep an immortal ticket that emails the owner.

Bruce's Londonderry 186 grew phantom "inverters" from Chint FlexOM data loggers
(0000e7be1902c000 / 000053571e02ca00 / 00005cad1f022a00). The 2026-08-09 fix
stopped them being created, and the rows were pruned on 2026-08-10 — but the
RepairTickets already opened against them stayed open forever:

  * reconcile skipped them (`info is None` → `continue`), because a deleted
    inverter never appears in the fleet tree and so can never reach the "ok"
    branch that clears a ticket, and
  * escalate_stale_repairs draws candidates from RepairTicket alone, with no
    join to Inverter — so on day 7 it emailed the owner that a data logger was
    a dead inverter (AO-TICKET-84, 2026-08-12).

Both layers now check whether the inverter is RETIRED (row soft-deleted or
gone). The safety property that matters just as much: a ticket whose inverter is
merely missing from a given fleet-tree build — a vendor timeout, a connection
blip — must NOT be closed, or a transient glitch would silently cancel a real
outage.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

import pytest

import api.repair_ops as ro
from api.db import SessionLocal, init_db
from api.models import Array, Inverter, RepairTicket, Tenant, now


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _tenant(**over) -> Tenant:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    fields = dict(
        id=tid, name="Retired Device Test", contact_email=f"{key}@owner.test",
        tenant_key=key, plan="comped", active=True, product="array_operator",
        repair_auto_open=True,
    )
    fields.update(over)
    with SessionLocal() as db:
        t = Tenant(**fields)
        db.add(t)
        db.commit()
        db.refresh(t)
        db.expunge(t)
    return t


def _array_and_inverter(tenant_id: str, *, deleted: bool = False):
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name="Londonderry 186")
        db.add(arr)
        db.flush()
        iv = Inverter(
            tenant_id=tenant_id, array_id=arr.id, vendor="chint",
            serial="000053571e02ca00", position=5, name="000053571e02ca00",
            deleted_at=now() if deleted else None,
        )
        db.add(iv)
        db.flush()
        aid, iid = arr.id, iv.id
        db.commit()
    return aid, iid


def _stale_ticket(tenant_id: str, array_id: int, inverter_id: int, **over) -> int:
    fields = dict(
        tenant_id=tenant_id, array_id=array_id, inverter_id=inverter_id,
        site_name="Londonderry 186", inv_name="000053571e02ca00",
        serial="000053571e02ca00", vendor="chint",
        title="Dead — Londonderry 186 / 000053571e02ca00",
        fail_type="dead", status="open", source="auto",
        opened_at=now() - timedelta(days=10),
    )
    fields.update(over)
    with SessionLocal() as db:
        t = RepairTicket(**fields)
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id


def _reload(ticket_id: int) -> RepairTicket:
    with SessionLocal() as db:
        return db.get(RepairTicket, ticket_id)


# ── the email chokepoint ──────────────────────────────────────────────────────

def test_retired_inverter_is_never_escalated_to_the_owner():
    """The exact Londonderry shape: ticket older than the escalation window,
    inverter soft-deleted. No email, and the case is closed out."""
    tenant = _tenant()
    aid, iid = _array_and_inverter(tenant.id, deleted=True)
    ticket_id = _stale_ticket(tenant.id, aid, iid)

    with patch("api.energy_agent_email.send_repair_escalation_email") as send:
        with SessionLocal() as db:
            sent = ro.escalate_stale_repairs(db, tenant)
            db.commit()

    assert send.call_count == 0
    assert sent == 0
    t = _reload(ticket_id)
    assert t.status == "cancelled"
    assert t.cancelled_at is not None


def test_live_inverter_still_escalates():
    """Regression guard — the retirement check must not silence real outages."""
    tenant = _tenant()
    aid, iid = _array_and_inverter(tenant.id, deleted=False)
    ticket_id = _stale_ticket(tenant.id, aid, iid)

    with patch("api.energy_agent_email.send_repair_escalation_email",
               return_value=True) as send:
        with SessionLocal() as db:
            sent = ro.escalate_stale_repairs(db, tenant)
            db.commit()

    assert send.call_count == 1
    assert sent == 1
    assert _reload(ticket_id).owner_escalated_at is not None


def test_escalation_guard_holds_when_auto_open_is_off():
    """reconcile returns early for repair_auto_open=False tenants, so the email
    layer is the only thing standing between a retired device and the owner."""
    tenant = _tenant(repair_auto_open=False)
    aid, iid = _array_and_inverter(tenant.id, deleted=True)
    _stale_ticket(tenant.id, aid, iid)

    with patch("api.energy_agent_email.send_repair_escalation_email") as send:
        with SessionLocal() as db:
            ro.escalate_stale_repairs(db, tenant)
            db.commit()

    assert send.call_count == 0


# ── reconcile ─────────────────────────────────────────────────────────────────

def test_reconcile_cancels_ticket_for_retired_inverter():
    tenant = _tenant()
    aid, iid = _array_and_inverter(tenant.id, deleted=True)
    ticket_id = _stale_ticket(tenant.id, aid, iid)

    # A retired inverter is absent from the fleet tree by construction.
    tree = {"columns": [{"array_id": aid, "array_name": "Londonderry 186",
                         "inverters": []}], "summary": {}}
    with SessionLocal() as db:
        res = ro.reconcile(db, tenant, tree=tree)
        db.commit()

    assert res["closed"] >= 1
    t = _reload(ticket_id)
    assert t.status == "cancelled"
    assert "retired from the fleet" in (t.tech_note or "")


def test_reconcile_leaves_ticket_open_when_inverter_merely_missing_from_tree():
    """THE safety property. A vendor timeout drops the array from one build; the
    inverter row is still live. The outage is real — never cancel it."""
    tenant = _tenant()
    aid, iid = _array_and_inverter(tenant.id, deleted=False)
    ticket_id = _stale_ticket(tenant.id, aid, iid)

    tree = {"columns": [], "summary": {}}
    with SessionLocal() as db:
        ro.reconcile(db, tenant, tree=tree)
        db.commit()

    t = _reload(ticket_id)
    assert t.status == "open"
    assert t.cancelled_at is None
