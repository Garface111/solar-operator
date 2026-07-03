"""Bruce's automatic invoice-time cross-check (2026-07).

POST /subscriptions/{id}/draft must run the GMP cross-check in the background
and return it WITH the draft: GMP's implied share for the offtaker (credited ÷
the array bill's group excess) vs the share the operator entered, flagged when
the variance exceeds SHARE_VARIANCE_THRESHOLD_PCT percentage points OR the
credited kWh misses share × pool beyond the audit tolerance. Fail-soft: when
the check can't run honestly the response carries crosscheck=null and the
draft still generates — never a fabricated verdict, never a blocked invoice.
"""
from __future__ import annotations

import pathlib
import secrets
from datetime import datetime

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, Client,
                        BillingReportSubscription)
from api.billing.reconcile_bills import (SHARE_VARIANCE_THRESHOLD_PCT,
                                         generation_crosscheck)

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"
BASE = "/v1/array-operator/billing"
RATE = 0.16
GROUP = 28772.0            # the array's group excess on its own (host) bill


def _auth(a):
    return {"Authorization": a}


def _seed_tenant_with_array() -> tuple[str, str, int]:
    """A tenant + array whose host GMP bill states the group excess pool."""
    tid = "ten_xchk_" + secrets.token_hex(4)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key="sol_live_" + secrets.token_urlsafe(12),
                      name="Xcheck Operator", contact_email=f"{tid}@operator.test",
                      active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Timberworks", region="VT")
        db.add(arr); db.flush()
        host = UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number="HOST", array_id=arr.id)
        db.add(host); db.flush()
        db.add(Bill(tenant_id=tid, account_id=host.id,
                    period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 30),
                    kwh_generated=28788, kwh_sent_to_grid=GROUP, is_net_metered=True))
        db.commit()
        return tid, f"Bearer {mint_session_for_tenant(tid)}", arr.id


def _seed_offtaker(tid: str, array_id: int, name: str, share: float,
                   credited: float) -> int:
    """A GMP-bound offtaker: own account + own bill showing GMP's credited excess."""
    with SessionLocal() as db:
        acct = UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number=name[:8], nickname=name)
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 30),
                    kwh_sent_to_grid=credited,
                    solar_credit_usd=round(credited * RATE, 2), is_net_metered=True))
        c = Client(tenant_id=tid, name=name, active=True); db.add(c); db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name=name, array_id=array_id,
            allocation_pct=1.0, array_share_pct=share, utility_account_id=acct.id,
            billing_model="percent_of_array", cadence="monthly")
        db.add(sub); db.commit()
        return sub.id


def test_crosscheck_within_threshold_rides_the_draft_response(client):
    """Clean allocation: 30% of 28,772 ≈ 8,631.6, GMP credited 8,632 → the draft
    response carries a non-flagged crosscheck with the real shares."""
    tid, auth, aid = _seed_tenant_with_array()
    sub_id = _seed_offtaker(tid, aid, "Fair Haven School", 0.30, 8632.0)
    r = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["draft"]["status"] == "pending"
    xc = body["crosscheck"]
    assert xc is not None
    assert xc["flagged"] is False
    assert xc["threshold_pct"] == SHARE_VARIANCE_THRESHOLD_PCT
    assert xc["entered_share_pct"] == 30.0
    assert abs(xc["computed_share_pct"] - 30.0014) < 0.001, xc
    assert abs(xc["variance_pct"]) < SHARE_VARIANCE_THRESHOLD_PCT
    assert xc["kwh_master"] == GROUP
    assert abs(xc["kwh_offtaker_expected"] - 8631.6) < 0.2
    assert xc["kwh_offtaker_credited"] == 8632.0


def test_crosscheck_flags_share_variance_beyond_threshold(client):
    """GMP effectively used ~25.02% where the operator entered 25.53% — the
    variance (~0.51 points) exceeds the 0.1-point threshold → flagged, with the
    real numbers for the warning strip."""
    tid, auth, aid = _seed_tenant_with_array()
    sub_id = _seed_offtaker(tid, aid, "St. J Muni", 0.2553, 7200.0)
    r = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth))
    assert r.status_code == 200, r.text
    xc = r.json()["crosscheck"]
    assert xc is not None and xc["flagged"] is True
    assert xc["entered_share_pct"] == 25.53
    assert abs(xc["computed_share_pct"] - 25.0243) < 0.001, xc
    assert abs(xc["variance_pct"]) > SHARE_VARIANCE_THRESHOLD_PCT
    # expected = 25.53% × 28,772 ≈ 7,345.5; GMP credited 7,200 → ~145.5 kWh off.
    assert abs(xc["kwh_offtaker_expected"] - 7345.5) < 0.3
    assert xc["kwh_offtaker_credited"] == 7200.0
    assert abs(xc["delta_kwh"] + 145.5) < 0.5, xc
    assert xc["delta_dollars"] is not None and xc["delta_dollars"] > 0


def test_crosscheck_flags_bruces_real_case_via_kwh_tolerance():
    """Bruce's actual worked example: GMP credited 7,343 kWh at a 25.53% share —
    an implied group total (28,762) on neither bill. The SHARE variance is only
    ~0.009 points (inside the 0.1 threshold), but the kWh delta (~2.5) exceeds
    the audit tolerance → still flagged. The invoice-time check must never be
    weaker than the audit sandbox that catches the $25 error."""
    tid, _auth_hdr, aid = _seed_tenant_with_array()
    sub_id = _seed_offtaker(tid, aid, "Brooks House", 0.2553, 7343.0)
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        xc = generation_crosscheck(db, sub)
    assert xc is not None
    assert abs(xc["variance_pct"]) < SHARE_VARIANCE_THRESHOLD_PCT  # share alone: quiet
    assert xc["flagged"] is True                                   # kWh tolerance: caught
    assert abs(xc["kwh_offtaker_expected"] - 7345.5) < 0.3
    assert xc["kwh_offtaker_credited"] == 7343.0


def test_crosscheck_null_when_it_cannot_run_draft_still_generates(client):
    """A workbook-based offtaker with no bound utility account: the cross-check
    has no GMP-credited figure to verify → crosscheck is null (present in the
    response, honestly empty) and the draft generates normally."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key="sol_live_" + secrets.token_urlsafe(12),
                      name="Null Xcheck Operator", contact_email=f"{tid}@operator.test",
                      active=True, product="array_operator"))
        db.commit()
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    data = (FIX / "norwich.xlsx").read_bytes()
    files = {"file": ("norwich.xlsx", data,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    sub_id = client.post(f"{BASE}/subscriptions", files=files,
                         headers=_auth(auth)).json()["subscription"]["id"]
    r = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["draft"]["status"] == "pending"
    assert "crosscheck" in body and body["crosscheck"] is None
