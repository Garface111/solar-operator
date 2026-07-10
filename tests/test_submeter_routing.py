"""Sub-meter auto-routing (Ford 2026-07-07).

When an offtaker binds to its OWN GMP sub-account — a meter DISTINCT from the
array's HOST meter, inside a net-metering group — that sub-account's own excess
ALREADY reflects the offtaker's metered share of the group. So the operator's
ONE entered share is routed to array_share_pct (the GROUP share that real_math
bills as share × group excess), and allocation_pct is pinned to 1.0 (bill 100%
of the sub-meter). This makes the double-count structurally impossible: the
sub-account's own excess is never ALSO multiplied by the entered share.

Percent-of-array offtakers (account IS the array host) are untouched: their
allocation_pct stays their share of the host meter.

The concrete failure this prevents: sub-meter excess 1000 kWh, group excess
5000 kWh, share 20%. Correct bill = 20% × 5000 = 1000 kWh (== the sub-meter, as
it should). The OLD double-count path billed 1000 × 0.20 = 200 kWh — a 5×
under-bill straight out of the bill audit.
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from sqlalchemy import select
from fastapi.testclient import TestClient

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill,
                        BillingReportSubscription)
from api.billing import delivery

_BASE = "/v1/array-operator/billing/subscriptions"


def _auth(tid):
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def _seed():
    """Tenant + one array in a net-metering group: a HOST account (lowest id,
    group excess 5000) and a separate SUB account (the offtaker's own meter,
    excess 1000). Bills carry kwh_sent_to_grid (excess) + solar_credit_usd."""
    tid = "ten_subm_" + secrets.token_hex(4)
    pe = date.today().replace(day=1) - timedelta(days=1)
    ps = pe.replace(day=1)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Submeter Operator",
                      contact_email=f"{tid}@op.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(12),
                      plan="standard", active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Group Array", fuel_type="solar")
        db.add(arr); db.flush()
        host = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="HOST-" + secrets.token_hex(2),
                              nickname="Host")
        db.add(host); db.flush()
        sub = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                             account_number="SUB-" + secrets.token_hex(2),
                             nickname="Sub")
        db.add(sub); db.flush()
        # HOST bill = the whole group: 5000 kWh excess.
        db.add(Bill(tenant_id=tid, account_id=host.id, bill_date=pe,
                    period_start=ps, period_end=pe, kwh_generated=6000,
                    kwh_sent_to_grid=5000.0,
                    solar_credit_usd=round(5000.0 * 0.25, 2)))
        # SUB bill = this offtaker's own metered share: 1000 kWh excess.
        db.add(Bill(tenant_id=tid, account_id=sub.id, bill_date=pe,
                    period_start=ps, period_end=pe, kwh_generated=1200,
                    kwh_sent_to_grid=1000.0,
                    solar_credit_usd=round(1000.0 * 0.25, 2)))
        db.commit()
        return tid, arr.id, host.id, sub.id


def _fetch(tid, sub_id):
    with SessionLocal() as db:
        return db.get(BillingReportSubscription, sub_id)


def test_submeter_create_routes_share_and_pins_alloc_to_one(client: TestClient):
    """Binding to the SUB account with an entered 30% share → stored
    allocation_pct == 1.0, array_share_pct == 0.30, and the invoice bills the
    SUB account's OWN bill (1000 kWh — Ford 2026-07-10: the sub-client bill
    governs) — NOT 0.30 × 5000 = 1500 (entered share is audit-only) and NOT
    the 1000 × 0.30 = 300 double-count."""
    tid, aid, host_id, sub_id = _seed()
    r = client.post(_BASE, headers=_auth(tid), data={
        "customer_name": "Sub Offtaker",
        "array_id": aid, "utility_account_id": sub_id,
        "allocation_pct": 0.30,
    })
    assert r.status_code == 200, r.text
    new_id = r.json()["subscription"]["id"]
    s = _fetch(tid, new_id)
    assert s.allocation_pct == 1.0           # pinned: 100% of the sub-meter
    assert abs(s.array_share_pct - 0.30) < 1e-9   # the ONE entered share → audit share
    # End-to-end: the invoice bills the own bill; share is the audit reference.
    ci = delivery.build_manual_match(s).computed_invoice
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 1000.0) < 0.5     # their own bill's excess
    assert abs(ci["kwh"] - 1500.0) > 400     # NOT entered share × pool
    assert abs(ci["kwh"] - 300.0) > 400      # NOT the double-count
    # The audit side-figures + the bill-derived share are all present.
    assert abs(ci["realmath_kwh"] - 1500.0) < 0.5
    assert abs(ci["derived_share_pct"] - 0.20) < 1e-6   # 1000 ÷ 5000, from the bills


def test_host_account_offtaker_is_untouched(client: TestClient):
    """Binding to the HOST account (percent-of-array) leaves allocation_pct as the
    entered share and array_share_pct unset — the existing behavior is preserved."""
    tid, aid, host_id, sub_id = _seed()
    r = client.post(_BASE, headers=_auth(tid), data={
        "customer_name": "Host Offtaker",
        "array_id": aid, "utility_account_id": host_id,
        "allocation_pct": 0.20,
    })
    assert r.status_code == 200, r.text
    s = _fetch(tid, r.json()["subscription"]["id"])
    assert abs(s.allocation_pct - 0.20) < 1e-9   # unchanged
    assert s.array_share_pct is None
    ci = delivery.build_manual_match(s).computed_invoice
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 1000.0) < 0.5         # 0.20 × 5000 host excess


def test_patch_rebind_to_submeter_enforces_invariant(client: TestClient):
    """Editing an offtaker onto a sub-account (or setting its share while bound to
    one) enforces the same invariant: share → array_share_pct, allocation_pct → 1.0."""
    tid, aid, host_id, sub_id = _seed()
    # Start as a host offtaker.
    r = client.post(_BASE, headers=_auth(tid), data={
        "customer_name": "Rebind Offtaker",
        "array_id": aid, "utility_account_id": host_id,
        "allocation_pct": 0.20,
    })
    oid = r.json()["subscription"]["id"]
    # Re-bind to the sub-meter with the entered share in the (legacy) share field.
    r2 = client.patch(f"{_BASE}/{oid}", headers=_auth(tid),
                      json={"utility_account_id": sub_id, "allocation_pct": 0.20})
    assert r2.status_code == 200, r2.text
    s = _fetch(tid, oid)
    assert s.allocation_pct == 1.0
    assert abs(s.array_share_pct - 0.20) < 1e-9
    ci = delivery.build_manual_match(s).computed_invoice
    assert ci["billing_basis"] == "gmp_credited"     # own sub bill governs
    assert abs(ci["kwh"] - 1000.0) < 0.5


def test_patch_share_edit_on_submeter_updates_audit_share_not_the_bill(client: TestClient):
    '''Editing a sub-metered offtaker's share (the single share field, sent as
    allocation_pct) routes to array_share_pct EVERY time — but it is the AUDIT
    share now (Ford 2026-07-10): the INVOICE keeps billing the sub account's
    own settled bill and must NOT move when the share is edited.'''
    tid, aid, host_id, sub_id = _seed()
    r = client.post(_BASE, headers=_auth(tid), data={
        'customer_name': 'Editable Sub', 'array_id': aid,
        'utility_account_id': sub_id, 'allocation_pct': 0.20})
    oid = r.json()['subscription']['id']
    before = delivery.build_manual_match(_fetch(tid, oid)).computed_invoice
    # bump the share to 30% via the (single) share field
    r2 = client.patch(f'{_BASE}/{oid}', headers=_auth(tid),
                      json={'allocation_pct': 0.30})
    assert r2.status_code == 200, r2.text
    s = _fetch(tid, oid)
    assert s.allocation_pct == 1.0
    assert abs(s.array_share_pct - 0.30) < 1e-9      # was 0.20, now 0.30 (not lost)
    ci = delivery.build_manual_match(s).computed_invoice
    assert ci['billing_basis'] == 'gmp_credited'     # own bill still governs
    assert abs(ci['kwh'] - 1000.0) < 0.5             # UNCHANGED: their bill's excess
    assert abs(ci['amount_owed'] - before['amount_owed']) < 0.01  # share edit ≠ money edit
    assert abs(ci['realmath_kwh'] - 1500.0) < 0.5    # audit figure follows the new share
