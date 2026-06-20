"""
Integration tests for VEC bill/usage persistence via /v1/sync.

When the extension POSTs bills + usage in the VEC payload, the server should
persist them as Bill rows linked to the tenant's utility accounts.  Covers:

  1. bills_raw alone → Bill rows created with parse_status="partial"
  2. bills_raw + matching usage → parse_status="parsed", kwh_generated set
  3. bills_raw without usage → parse_status="partial", kwh_generated=None
  4. Second sync with identical bills is idempotent (no duplicate rows)
  5. Bills whose account_id is not in the tenant's accounts are silently dropped
"""
from __future__ import annotations

import json
import pathlib
import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Bill, Tenant

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vec"


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="VEC Sync Test", contact_email="vs@test.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _vec_payload(
    accounts: list,
    bills: list | None = None,
    usage: list | None = None,
) -> dict:
    return {
        "provider": "vec",
        "user": {"email": "op@vs.test", "username": "op@vs.test"},
        "auth": {},
        "accounts": accounts,
        "bills": bills or [],
        "usage": usage or [],
    }


def _sync(client, key: str, payload: dict):
    return client.post(
        "/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"}
    )


# ── (1) bills_raw persists two Bill rows ────────────────────────────────────────

def test_bills_raw_creates_bill_rows(client):
    tid, key = _make_tenant()
    bills_data = json.loads((FIXTURES / "billing_rows.json").read_text())

    resp = _sync(client, key, _vec_payload(
        accounts=[{"accountNumber": "6578300", "customerName": "Test LLC"}],
        bills=bills_data,
    ))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        bills = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
    assert len(bills) == 2
    assert all(b.parse_status == "partial" for b in bills)


# ── (2) Matching usage → fully parsed bill ──────────────────────────────────────

def test_bills_raw_with_matching_usage_creates_parsed_bill(client):
    """Bill billing_date "11/15/2023" (2023-11) matches usage period_end in Nov 2023."""
    tid, key = _make_tenant()
    bills_data = json.loads((FIXTURES / "billing_rows.json").read_text())[:1]

    usage_data = [{
        "aria_label": (
            "Nov 2023 Billing Period. Usage Dates: Oct 18 - Nov 17. "
            "Meter 63698951 - Consumption - kWh: 1234 kWh. Average Temperature: 45 °F"
        ),
        "account_id": "6578300",
    }]

    resp = _sync(client, key, _vec_payload(
        accounts=[{"accountNumber": "6578300", "customerName": "Test LLC"}],
        bills=bills_data,
        usage=usage_data,
    ))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        bills = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
    assert len(bills) == 1
    b = bills[0]
    assert b.parse_status == "parsed"
    # SmartHub/VEC bill kWh is CONSUMPTION (net usage), not generation — it must
    # route to kwh_consumed and NEVER touch kwh_generated (see app.py sync path:
    # writing it to kwh_generated zeroed every VEC/WEC NEPOOL report). Generation
    # comes only from the daily utility-usage net-export pull.
    assert b.kwh_consumed == 1234
    assert b.kwh_generated is None
    assert b.period_start is not None
    assert b.period_end is not None


# ── (3) Bills without usage → partial status ────────────────────────────────────

def test_bills_raw_without_usage_creates_partial_bill(client):
    tid, key = _make_tenant()
    bills_data = json.loads((FIXTURES / "billing_rows.json").read_text())[:1]

    resp = _sync(client, key, _vec_payload(
        accounts=[{"accountNumber": "6578300", "customerName": "Test LLC"}],
        bills=bills_data,
    ))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        bills = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
    assert len(bills) == 1
    b = bills[0]
    assert b.parse_status == "partial"
    assert b.kwh_generated is None


# ── (4) Idempotency: second sync with same bills adds nothing ───────────────────

def test_bills_raw_is_idempotent(client):
    tid, key = _make_tenant()
    bills_data = json.loads((FIXTURES / "billing_rows.json").read_text())[:1]
    accounts = [{"accountNumber": "6578300", "customerName": "Test LLC"}]

    assert _sync(client, key, _vec_payload(accounts, bills_data)).status_code == 200
    assert _sync(client, key, _vec_payload(accounts, bills_data)).status_code == 200

    with SessionLocal() as db:
        bills = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
    assert len(bills) == 1


# ── (5) Bills for unknown accounts are silently dropped ─────────────────────────

def test_bills_for_unknown_account_skipped(client):
    """Bill rows whose account_id has no matching UtilityAccount are ignored."""
    tid, key = _make_tenant()
    unknown_bill = [{
        "account_id": "UNKNOWN_ACCT",
        "billing_date": "01/15/2024",
        "bill_amount": "-100.00",
        "bill_uuid": "uuid-unknown-" + secrets.token_hex(4),
    }]

    resp = _sync(client, key, _vec_payload(
        accounts=[{"accountNumber": "6578300", "customerName": "Test LLC"}],
        bills=unknown_bill,
    ))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        bills = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
    assert len(bills) == 0


# ── (6) UPDATE-don't-skip: a later capture backfills kWh on a partial bill ──────

def test_later_usage_backfills_kwh_on_existing_bill(client):
    """The exact VEC failure: bill first lands from the billing-history page with
    NO kWh (partial). A LATER sync that includes the Usage Explorer reading for
    the same month must UPDATE the existing bill's kwh_generated, not skip it as
    a duplicate (which left every VEC bill stuck at 0)."""
    tid, key = _make_tenant()
    bills_data = json.loads((FIXTURES / "billing_rows.json").read_text())[:1]
    accounts = [{"accountNumber": "6578300", "customerName": "Test LLC"}]

    # First sync: billing-history only → partial bill, kwh None.
    assert _sync(client, key, _vec_payload(accounts, bills_data)).status_code == 200
    with SessionLocal() as db:
        b = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().one()
        assert b.parse_status == "partial"
        assert b.kwh_generated is None

    # Second sync: SAME bill + the Usage Explorer row for that month (Nov 2023).
    usage_data = [{
        "aria_label": (
            "Nov 2023 Billing Period. Usage Dates: Oct 18 - Nov 17. "
            "Meter 63698951 - Consumption - kWh: 1234 kWh. Average Temperature: 45 °F"
        ),
        "account_id": "6578300",
    }]
    assert _sync(client, key, _vec_payload(accounts, bills_data, usage_data)).status_code == 200

    with SessionLocal() as db:
        bills = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
    # Still ONE bill (no duplicate), now backfilled with the real kWh — which is
    # CONSUMPTION (kwh_consumed), never kwh_generated (SmartHub/VEC bill kWh is net
    # usage; routing it to kwh_generated zeroed every VEC/WEC NEPOOL report).
    assert len(bills) == 1
    b = bills[0]
    assert b.kwh_consumed == 1234
    assert b.kwh_generated is None
    assert b.parse_status == "parsed"
