"""Tests for the 'copy me on every report' tenant setting.

When tenant.cc_on_reports is True, delivery.deliver_for_client sends the operator
an identical workbook email with a '[copy] ' subject prefix — in addition to the
client's own copy.
"""
from __future__ import annotations

import secrets
from datetime import datetime

import pytest

from api.db import SessionLocal, init_db
from api.models import Tenant, Client, Array, UtilityAccount, Bill, now
from api import delivery


def _seed_tenant(cc_on_reports: bool, *, client_email: str | None,
                 tenant_email: str) -> tuple[str, int]:
    """Create a comped tenant + one client + one array w/ a bill. Returns
    (tenant_id, client_id)."""
    init_db()
    tid = "ten_test_" + secrets.token_hex(6)
    with SessionLocal() as db:
        t = Tenant(
            id=tid, name="Test Operator", contact_email=tenant_email,
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, subscription_status="comped",
            report_frequency="quarterly", cc_on_reports=cc_on_reports,
        )
        db.add(t); db.flush()
        c = Client(tenant_id=tid, name="Acme Solar LLC",
                   contact_email=client_email, active=True)
        db.add(c); db.flush()
        cid = c.id
        a = Array(tenant_id=tid, client_id=cid, name="North Field",
                  nepool_gis_id="99001", bill_offset_months=1)
        db.add(a); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider="gmp",
                              account_number=f"acct-{secrets.token_hex(3)}",
                              nickname="North Field")
        db.add(acct); db.flush()
        # One recent bill so the workbook has data to render.
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    bill_date=datetime(2026, 1, 15),
                    period_start=datetime(2026, 1, 1),
                    period_end=datetime(2026, 1, 31),
                    billing_days=31, kwh_generated=20000,
                    document_number=f"{tid}-doc-1", parse_status="parsed"))
        db.commit()
    return tid, cid


@pytest.fixture()
def captured_emails(monkeypatch):
    """Capture every send_workbook_email call (avoids real Resend sends)."""
    sent: list[dict] = []

    def fake_send(to, subject, html, text, workbook_path, filename=None):
        sent.append({"to": to, "subject": subject, "filename": filename})
        return True

    # Patch the name imported into the delivery module.
    monkeypatch.setattr(delivery, "send_workbook_email", fake_send)
    return sent


def test_copy_sent_with_prefix_when_toggle_on(captured_emails):
    tid, cid = _seed_tenant(
        cc_on_reports=True,
        client_email="client@acme.example",
        tenant_email="operator@op.example",
    )
    res = delivery.deliver_for_client(cid, triggered_by="self-serve")
    assert res["ok"] is True

    tos = [e["to"] for e in captured_emails]
    assert "client@acme.example" in tos      # client's own copy
    assert "operator@op.example" in tos       # operator's [copy]

    copy = [e for e in captured_emails if e["to"] == "operator@op.example"]
    assert len(copy) == 1
    assert copy[0]["subject"].startswith("[copy] ")


def test_no_copy_when_toggle_off(captured_emails):
    tid, cid = _seed_tenant(
        cc_on_reports=False,
        client_email="client2@acme.example",
        tenant_email="operator2@op.example",
    )
    delivery.deliver_for_client(cid, triggered_by="self-serve")
    tos = [e["to"] for e in captured_emails]
    assert "client2@acme.example" in tos
    assert "operator2@op.example" not in tos


def test_no_duplicate_when_tenant_is_already_recipient(captured_emails):
    """If the client has no own email, the report already goes to the tenant
    address as primary — the [copy] must not double-send it."""
    tid, cid = _seed_tenant(
        cc_on_reports=True,
        client_email=None,                      # falls back to tenant email
        tenant_email="solo@op.example",
    )
    delivery.deliver_for_client(cid, triggered_by="self-serve")
    to_solo = [e for e in captured_emails if e["to"] == "solo@op.example"]
    assert len(to_solo) == 1
    assert not to_solo[0]["subject"].startswith("[copy] ")
