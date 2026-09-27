"""One owner escalation email per site, with a subject that names the unit.

On 2026-07-24 Tinker Hall's owners each got three "Tinker Hall Site has been
down 7 days — want me to get it fixed?" emails in the same second, one per
inverter (13, 14, 15), when one unit was dead and two were underperforming.
The site was never down.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

import pytest

import api.repair_ops as ro
from api.db import SessionLocal, init_db
from api.models import RepairTicket, Tenant, now


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _tenant() -> Tenant:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    t = Tenant(
        id=tid, name="One Per Site", contact_email=f"{key}@owner.test",
        tenant_key=key, plan="comped", active=True, product="array_operator",
    )
    with SessionLocal() as db:
        db.add(t)
        db.commit()
        db.refresh(t)
        db.expunge(t)
    return t


def _ticket(tenant_id: str, site: str, inv: str, fail: str, days: int = 10) -> int:
    with SessionLocal() as db:
        t = RepairTicket(
            tenant_id=tenant_id, title=f"{inv} {fail}", fail_type=fail, status="open",
            site_name=site, inv_name=inv, opened_at=now() - timedelta(days=days),
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id


def test_three_stale_inverters_at_one_site_send_one_email():
    tenant = _tenant()
    ids = [
        _ticket(tenant.id, "Tinker Hall Site", "Inverter 13", "underperforming", 12),
        _ticket(tenant.id, "Tinker Hall Site", "Inverter 14", "underperforming", 11),
        _ticket(tenant.id, "Tinker Hall Site", "Inverter 15", "dead", 9),
    ]
    with patch("api.energy_agent_email.send_repair_escalation_email", return_value=True) as send:
        with SessionLocal() as db:
            sent = ro.escalate_stale_repairs(db, tenant)
            db.commit()
    assert sent == 1
    assert send.call_count == 1
    kw = send.call_args.kwargs
    assert kw["inverter"] == "Inverter 15"          # the dead one leads
    assert kw["fail_type"] == "dead"
    assert {a["inverter"] for a in kw["also"]} == {"Inverter 13", "Inverter 14"}
    with SessionLocal() as db:
        for tid in ids:
            assert db.get(RepairTicket, tid).owner_escalated_at is not None


def test_two_sites_get_two_emails():
    tenant = _tenant()
    _ticket(tenant.id, "Benson Site", "Inverter 02", "dead")
    _ticket(tenant.id, "Tinker Hall Site", "Inverter 15", "dead")
    with patch("api.energy_agent_email.send_repair_escalation_email", return_value=True) as send:
        with SessionLocal() as db:
            assert ro.escalate_stale_repairs(db, tenant) == 2
            db.commit()
    assert send.call_count == 2


def test_failed_send_leaves_whole_site_unescalated():
    tenant = _tenant()
    ids = [
        _ticket(tenant.id, "Tinker Hall Site", "Inverter 14", "underperforming"),
        _ticket(tenant.id, "Tinker Hall Site", "Inverter 15", "dead"),
    ]
    with patch("api.energy_agent_email.send_repair_escalation_email", return_value=False):
        with SessionLocal() as db:
            assert ro.escalate_stale_repairs(db, tenant) == 0
            db.commit()
    with SessionLocal() as db:
        for tid in ids:
            assert db.get(RepairTicket, tid).owner_escalated_at is None


def test_subject_names_the_unit_not_a_down_site():
    from api import energy_agent_email as eae
    tenant = _tenant()
    captured = {}

    def fake_send(**kw):
        captured.update(kw)
        return True

    with patch("api.notify._send_via_resend", side_effect=fake_send):
        ok = eae.send_repair_escalation_email(
            tenant, ticket_id=1, site="Tinker Hall Site", inverter="Inverter 14",
            fail_type="underperforming", diagnosis="33% below peers", days_down=7,
        )
    assert ok
    subj = captured["subject"]
    assert subj == "Tinker Hall Site: Inverter 14 has been underperforming its peers for 7 days"
    assert "has been down" not in subj and "want me" not in subj
    assert "has been down" not in captured["text"]


def test_grouped_subject_counts_inverters_and_lists_the_rest():
    from api import energy_agent_email as eae
    tenant = _tenant()
    captured = {}
    with patch("api.notify._send_via_resend", side_effect=lambda **kw: captured.update(kw) or True):
        eae.send_repair_escalation_email(
            tenant, ticket_id=2, site="Tinker Hall Site", inverter="Inverter 15",
            fail_type="dead", diagnosis="no production", days_down=9,
            also=[{"inverter": "Inverter 13", "fail_type": "underperforming"},
                  {"inverter": "Inverter 14", "fail_type": "underperforming"}],
        )
    assert captured["subject"] == "Tinker Hall Site: 3 inverters need attention (9+ days)"
    assert "Also at Tinker Hall Site:" in captured["text"]
    assert "Inverter 13 has been underperforming its peers" in captured["text"]
