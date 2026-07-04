"""Weekly freshness scorecard (api/jobs/freshness_scorecard).

The scorecard is the measured answer to "was the data there when the product
needed it" — pure read over DailyGeneration / Inverter / PortalLoginStatus /
digest holds. These tests seed real rows and check the arithmetic and the
rendered text, so the Monday email can be trusted.
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from api.db import SessionLocal
from api.jobs import freshness_scorecard as sc
from api.models import (
    Array,
    DailyGeneration,
    Inverter,
    PortalLoginStatus,
    Tenant,
    now,
)


def _mk_tenant(**over) -> str:
    tid = "ten_" + secrets.token_hex(6)
    fields = dict(id=tid, name=f"Score {tid[-4:]}", contact_email=f"{tid}@x.test",
                  tenant_key="sol_" + secrets.token_hex(8), plan="standard", active=True)
    fields.update(over)
    with SessionLocal() as db:
        db.add(Tenant(**fields))
        db.commit()
    return tid


def _mk_array(tid: str, name: str, **over) -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, name=name, fuel_type="solar", **over)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id


def _add_days(tid: str, aid: int, days_ago: list[int], as_of: date) -> None:
    with SessionLocal() as db:
        for n in days_ago:
            db.add(DailyGeneration(tenant_id=tid, array_id=aid,
                                   day=as_of - timedelta(days=n), kwh=100.0))
        db.commit()


def test_coverage_math_and_per_tenant_ranking():
    as_of = now().date()
    tid = _mk_tenant(company_name="Coverage Co")
    full = _mk_array(tid, "Full")      # all 7 complete days covered
    half = _mk_array(tid, "Half")      # 3 of 7
    _mk_array(tid, "Empty")            # 0 of 7
    _add_days(tid, full, [1, 2, 3, 4, 5, 6, 7], as_of)
    _add_days(tid, half, [1, 2, 3], as_of)

    card = sc.build_scorecard(as_of=as_of)
    mine = [r for r in card["per_tenant"] if r["tenant"] == "Coverage Co"][0]
    assert mine["arrays"] == 3
    # (7 + 3 + 0) / 21 = 47.6%
    assert mine["coverage_pct"] == 47.6
    # Excluded / deleted arrays never count against coverage.
    _mk_array(tid, "Hidden", excluded=True)
    card2 = sc.build_scorecard(as_of=as_of)
    mine2 = [r for r in card2["per_tenant"] if r["tenant"] == "Coverage Co"][0]
    assert mine2["arrays"] == 3


def test_login_health_and_digest_holds_counted():
    tid = _mk_tenant(company_name="Login Co")
    with SessionLocal() as db:
        db.add(PortalLoginStatus(tenant_id=tid, provider="gmp", username="ok@x.com",
                                 username_lc="ok@x.com", enabled=True, paused=False,
                                 fails=0, last_ok_at=now()))
        db.add(PortalLoginStatus(tenant_id=tid, provider="gmp", username="bad@x.com",
                                 username_lc="bad@x.com", enabled=True, paused=True,
                                 fails=3, last_ok_at=None))
        t = db.get(Tenant, tid)
        t.digest_hold_notified_at = now()
        db.commit()

    card = sc.build_scorecard()
    assert card["utility_logins"]["automated"] >= 1
    assert card["utility_logins"]["failing"] >= 1
    assert "Login Co" in card["digest_holds"]

    text = sc._render_text(card)
    assert "FRESHNESS SCORECARD" in text
    assert "FAILING" in text
    assert "Login Co" in text


def test_live_source_snapshot_counts_only_reporting_inverters():
    tid = _mk_tenant(company_name="Live Co")
    aid = _mk_array(tid, "LiveArr")
    with SessionLocal() as db:
        db.add(Inverter(tenant_id=tid, array_id=aid, name="fresh", vendor="solaredge", serial="SN-F1",
                        source_last_data_at=now()))
        db.add(Inverter(tenant_id=tid, array_id=aid, name="stale", vendor="fronius", serial="SN-S1",
                        source_last_data_at=now() - timedelta(days=3)))
        db.add(Inverter(tenant_id=tid, array_id=aid, name="no-feed", vendor="chint", serial="SN-N1",
                        source_last_data_at=None))
        db.commit()

    card = sc.build_scorecard()
    # no-feed inverters aren't live sources; fresh counts, stale doesn't.
    assert card["live_sources_total"] >= 2
    assert card["live_sources_fresh"] >= 1
