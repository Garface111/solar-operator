"""Operator-chosen billing period for the offtaker draft (Bruce 2026-07-07, C4).

The draft flow auto-picked the latest settled bill and the operator had no way
to draft an EARLIER period. GET /subscriptions/{id}/bill-periods lists the
billable periods (newest first, flagging the implicit latest), and
POST /subscriptions/{id}/draft?period=YYYY-MM pins one — so the operator drives
which billing cycle the invoice covers instead of always getting the latest.
"""
from __future__ import annotations

import secrets
from datetime import datetime

import pytest

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, Client,
                        BillingReportSubscription)

BASE = "/v1/array-operator/billing"
RATE = 0.16

# The conftest DB is shared across the whole session (schema created once, never
# reset between tests), and a sibling test asserts the subscriptions table is
# empty. Clean up every tenant/subscription this module seeds so it never leaks
# rows into that order-dependent assertion.
_SEEDED_TENANTS: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup_seeded():
    yield
    tids = list(_SEEDED_TENANTS)
    _SEEDED_TENANTS.clear()
    if not tids:
        return
    # Best-effort hygiene: purge every row this module seeded so it never leaks
    # into a sibling's order-dependent assertion (test_match_preview_saves_nothing
    # asserts the subscriptions table is empty). Ordering the full FK graph is
    # brittle, so drop FK enforcement for this teardown-only connection (SQLite)
    # and delete each tenant's rows across every tenant-scoped table + tenants.
    from sqlalchemy import inspect as _inspect
    from api.db import engine
    from api.models import Base, Tenant
    try:
        existing = set(_inspect(engine).get_table_names())
        with engine.begin() as conn:
            try:
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            except Exception:
                pass
            for table in reversed(Base.metadata.sorted_tables):
                # Only touch tables the test schema actually created (the model
                # declares more than a minimal test DB materializes).
                if table.name in existing and "tenant_id" in table.columns:
                    conn.execute(table.delete().where(table.c.tenant_id.in_(tids)))
            conn.execute(Tenant.__table__.delete().where(Tenant.id.in_(tids)))
    except Exception:
        pass


def _auth(a):
    return {"Authorization": a}


def _seed_multi_period_offtaker(cadence: str = "monthly") -> tuple[str, str, int]:
    """A GMP-bound offtaker whose account has THREE settled monthly bills with
    excess (Oct/Nov/Dec 2025), plus a zero-excess month that must NOT be offered."""
    tid = "ten_period_" + secrets.token_hex(4)
    _SEEDED_TENANTS.append(tid)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key="sol_live_" + secrets.token_urlsafe(12),
                      name="Period Operator", contact_email=f"{tid}@operator.test",
                      active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Chester", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number="ACCT", nickname="Chester", array_id=arr.id)
        db.add(acct); db.flush()
        # Three billable months + one zero-excess month (banked → not offered).
        months = [
            (datetime(2025, 10, 1), datetime(2025, 10, 31), 5000.0),
            (datetime(2025, 11, 1), datetime(2025, 11, 30), 4200.0),
            (datetime(2025, 12, 16), datetime(2026, 1, 2), 3900.0),  # Bruce's real span
            (datetime(2025, 9, 1),  datetime(2025, 9, 30), 0.0),     # banked → skipped
        ]
        for ps, pe, excess in months:
            db.add(Bill(tenant_id=tid, account_id=acct.id,
                        period_start=ps, period_end=pe,
                        kwh_generated=excess + 1000,
                        kwh_sent_to_grid=excess,
                        solar_credit_usd=round(excess * RATE, 2),
                        is_net_metered=True))
        c = Client(tenant_id=tid, name="Fair Haven School", active=True)
        db.add(c); db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="Fair Haven School",
            array_id=arr.id, allocation_pct=0.5, utility_account_id=acct.id,
            billing_model="percent_of_array", cadence=cadence)
        db.add(sub); db.commit()
        return tid, f"Bearer {mint_session_for_tenant(tid)}", sub.id


def test_bill_periods_lists_billable_months_newest_first(client):
    tid, auth, sub_id = _seed_multi_period_offtaker()
    r = client.get(f"{BASE}/subscriptions/{sub_id}/bill-periods", headers=_auth(auth))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["cadence"] == "monthly"
    labels = [p["label"] for p in body["periods"]]
    # Newest first; the zero-excess Sept month is excluded.
    assert labels == ["2026-01", "2025-11", "2025-10"]
    assert body["periods"][0]["is_latest"] is True
    assert body["periods"][0]["pretty"] == "January 2026"
    assert all("is_latest" not in p for p in body["periods"][1:])


def test_draft_targets_the_chosen_period(client):
    tid, auth, sub_id = _seed_multi_period_offtaker()
    # Default (no period) → the latest bill (Dec 16 → Jan 2, labelled by end month).
    r_latest = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth))
    assert r_latest.status_code == 200, r_latest.text
    d_latest = r_latest.json()["draft"]
    assert "2026-01-02" in (d_latest["period_label"] or "")

    # Explicit earlier period → that period's bill drives the draft.
    r_oct = client.post(f"{BASE}/subscriptions/{sub_id}/draft?period=2025-10",
                        headers=_auth(auth))
    assert r_oct.status_code == 200, r_oct.text
    d_oct = r_oct.json()["draft"]
    assert "2025-10" in (d_oct["period_label"] or ""), d_oct["period_label"]
    # Chester 50% of 5,000 excess = 2,500 kWh for October.
    assert abs((d_oct["customer_kwh"] or 0) - 2500.0) < 1.0, d_oct


def test_choosing_a_period_does_not_supersede_other_pending_drafts(client):
    """A historical pick holds side by side; only a latest/default draft prunes
    the others (the anti-stale-draft rule)."""
    tid, auth, sub_id = _seed_multi_period_offtaker()
    client.post(f"{BASE}/subscriptions/{sub_id}/draft?period=2025-10", headers=_auth(auth))
    client.post(f"{BASE}/subscriptions/{sub_id}/draft?period=2025-11", headers=_auth(auth))
    # Both explicit-period drafts remain pending.
    from api.models import ReportDraft
    from sqlalchemy import select
    with SessionLocal() as db:
        pend = db.execute(
            select(ReportDraft).where(ReportDraft.subscription_id == sub_id,
                                      ReportDraft.status == "pending")
        ).scalars().all()
    labels = sorted((d.period_label or "") for d in pend)
    assert len(pend) == 2, [d.period_label for d in pend]
    assert any("2025-10" in x for x in labels)
    assert any("2025-11" in x for x in labels)


def test_bill_periods_empty_for_workbook_offtaker(client):
    """No bound utility account → nothing to choose; empty list keeps the
    implicit latest-bill default."""
    tid = "ten_" + secrets.token_hex(6)
    _SEEDED_TENANTS.append(tid)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key="sol_live_" + secrets.token_urlsafe(12),
                      name="WB Operator", contact_email=f"{tid}@operator.test",
                      active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="WB Cust", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Arr", region="VT"); db.add(arr); db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="WB Cust",
            array_id=arr.id, allocation_pct=0.5, billing_model="percent_of_array",
            cadence="monthly")
        db.add(sub); db.commit()
        sub_id = sub.id
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    r = client.get(f"{BASE}/subscriptions/{sub_id}/bill-periods", headers=_auth(auth))
    assert r.status_code == 200, r.text
    assert r.json()["periods"] == []
