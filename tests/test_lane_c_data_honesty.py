"""Lane-C data-honesty fixes (SHARED-BACKLOG 2026-07-01/06-26 items).

1. Fleet-LOCAL day bucketing for capture writes + "today" reads (the UTC
   evening mis-slot that double-counted kWh into the Stripe meter).
2. Per-array daylight gating in build_fleet_tree (was central-VT for ALL).
3. /extension-status accepts a SESSION bearer like every other endpoint.
4. Bill→daily transform fires right after a bill pull (bills-only tenants
   must not read all-zeros until the nightly cron).
5. A bills-only tenant's overview shows honestly-labeled bill-derived kWh.
"""
import os, tempfile
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))

import secrets
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

from api.db import SessionLocal, init_db
from api.models import (Array, Bill, DailyGeneration, Tenant, UtilityAccount,
                        local_today)
from api import array_owners as AO
from api import inverter_fleet as IF


def setup_module(m):
    init_db()


def _mk_tenant(db, product="array_operator"):
    t = Tenant(id="ten_" + secrets.token_hex(6), name="Lane C Test",
               contact_email="lanec@example.com",
               tenant_key="sol_live_" + secrets.token_hex(6),
               product=product, active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


# ── 1. fleet-local day bucketing ──────────────────────────────────────────────

def test_local_today_is_fleet_local_not_utc():
    # 01:30 UTC on Jul 2 = 9:30 PM ET on Jul 1 → the fleet-local day is Jul 1.
    assert local_today(datetime(2026, 7, 2, 1, 30)) == date(2026, 7, 1)
    # Midday UTC = morning ET, same calendar day.
    assert local_today(datetime(2026, 7, 2, 12, 0)) == date(2026, 7, 2)
    # Winter (EST, UTC-5): 02:00 UTC Jan 15 = 9 PM ET Jan 14.
    assert local_today(datetime(2026, 1, 15, 2, 0)) == date(2026, 1, 14)


def test_evening_capture_lands_on_local_day_never_preseeds_tomorrow(monkeypatch):
    """The billing-safety proof for the UTC write-key fix: a 9:30 PM ET capture
    (already 'tomorrow' in UTC) must write energy_today into the CURRENT local
    day's row — never pre-seed tomorrow's slot with today's total (the old
    behavior, which climb-only then froze over a cloudier real tomorrow,
    double-counting kWh in every consumer incl. the Stripe usage meter)."""
    with SessionLocal() as db:
        t = _mk_tenant(db)
        tid = t.id

    d = date(2026, 7, 1)               # fleet-local day of the evening capture
    utc_d = date(2026, 7, 2)           # what utcnow().date() would have said
    monkeypatch.setattr(AO, "local_today", lambda: d)

    body = AO.InverterCaptureBody(provider="fronius", sites=[AO.CaptureSite(
        site_id="pv-lane-c", name="Lane C Evening", peak_power_kw=150.0,
        energy_today_kwh=900.0)])
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        AO._inverter_capture_for_tenant(t, "fronius", body)

    with SessionLocal() as db:
        rows = db.query(DailyGeneration).filter(
            DailyGeneration.tenant_id == tid).all()
        assert len(rows) == 1
        assert rows[0].day == d
        assert rows[0].kwh == 900.0
        # THE regression guard: tomorrow's slot was NOT pre-seeded.
        assert not any(r.day == utc_d for r in rows)

    # Same-evening re-capture climbs the SAME local day, still no tomorrow row.
    body2 = AO.InverterCaptureBody(provider="fronius", sites=[AO.CaptureSite(
        site_id="pv-lane-c", name="Lane C Evening", peak_power_kw=150.0,
        energy_today_kwh=950.0)])
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        AO._inverter_capture_for_tenant(t, "fronius", body2)
    with SessionLocal() as db:
        rows = db.query(DailyGeneration).filter(
            DailyGeneration.tenant_id == tid).all()
        assert len(rows) == 1 and rows[0].day == d and rows[0].kwh == 950.0


# ── 2. per-array daylight ────────────────────────────────────────────────────

def test_is_daylight_respects_coordinates():
    # 16:00 UTC Jul 2 = noon in Vermont (day) = midnight in Perth (night).
    when = datetime(2026, 7, 2, 16, 0)
    assert IF._is_daylight(44.26, -72.58, when=when) is True     # Vermont
    assert IF._is_daylight(-31.95, 115.86, when=when) is False   # Perth, AUS


def test_daylight_for_uses_array_coords_and_falls_back(monkeypatch):
    calls = []
    def fake_is_daylight(lat=None, lon=None, when=None):
        calls.append((lat, lon))
        return False
    monkeypatch.setattr(IF, "_is_daylight", fake_is_daylight)
    # With coords → the per-site calc runs and its verdict wins over the default.
    arr = SimpleNamespace(latitude=-31.95, longitude=115.86)
    assert IF._daylight_for(arr, default=True) is False
    assert calls == [(-31.95, 115.86)]
    # Without coords → regional default, no calc.
    calls.clear()
    assert IF._daylight_for(SimpleNamespace(latitude=None, longitude=None),
                            default=True) is True
    assert calls == []


def test_fleet_tree_emits_per_array_daylight(monkeypatch):
    """An array with far-away coords gets ITS OWN is_daylight, not Vermont's."""
    monkeypatch.setattr(
        IF, "_is_daylight",
        lambda lat=None, lon=None, when=None: lat is None)  # regional=True, coords=False
    with SessionLocal() as db:
        t = _mk_tenant(db)
        a_vt = Array(tenant_id=t.id, name="VT no coords", fuel_type="solar")
        a_far = Array(tenant_id=t.id, name="Perth far", fuel_type="solar",
                      latitude=-31.95, longitude=115.86)
        db.add_all([a_vt, a_far]); db.commit()
        tree = IF.build_fleet_tree(db, t)
    by_name = {c["array_name"]: c for c in tree["columns"]}
    assert by_name["VT no coords"]["is_daylight"] is True
    assert by_name["Perth far"]["is_daylight"] is False
    assert tree["summary"]["is_daylight"] is True  # board-wide stays regional


# ── 3. extension-status session bearer ───────────────────────────────────────

def test_extension_status_accepts_session_and_tenant_key():
    from api.account import _sign_session
    with SessionLocal() as db:
        t = _mk_tenant(db)
        tid, tkey = t.id, t.tenant_key
    # SPA session bearer (previously 403 "Invalid tenant key").
    out = AO.extension_status(authorization=f"Bearer {_sign_session(tid)}")
    assert out["product"] == "array_operator" and out["connected"] is True
    # Extension popup's raw tenant-key bearer still works.
    out2 = AO.extension_status(authorization=f"Bearer {tkey}")
    assert out2["product"] == "array_operator"


# ── 4. bill pull triggers the bill→daily transform ───────────────────────────

def test_pull_bills_triggers_bill_to_daily(monkeypatch):
    from api import worker
    from api.jobs import bill_to_daily
    called = {}
    monkeypatch.setattr(bill_to_daily, "transform_tenant_bills",
                        lambda tenant_id, db=None: called.setdefault("tid", tenant_id))
    with SessionLocal() as db:
        t = _mk_tenant(db)
        tid = t.id
    worker.pull_bills_for_tenant(tid)
    assert called.get("tid") == tid


# ── 5. bills-only tenant lights up honestly ──────────────────────────────────

def test_bills_only_tenant_overview_shows_labeled_estimate():
    """A tenant with GMP bills and ZERO inverter telemetry must not read
    all-zeros/no_source: after the bill→daily transform its overview carries
    the bill-derived kWh, explicitly flagged has_estimated (never presented
    as measured)."""
    from api.jobs.bill_to_daily import transform_tenant_bills
    with SessionLocal() as db:
        t = _mk_tenant(db)
        arr = Array(tenant_id=t.id, name="Bills Only Array", fuel_type="solar")
        db.add(arr); db.commit(); db.refresh(arr)
        ua = UtilityAccount(tenant_id=t.id, provider="gmp",
                            account_number="12345", array_id=arr.id)
        db.add(ua); db.commit(); db.refresh(ua)
        end = local_today() - timedelta(days=10)
        start = end - timedelta(days=29)
        db.add(Bill(tenant_id=t.id, account_id=ua.id,
                    period_start=datetime.combine(start, dtime.min),
                    period_end=datetime.combine(end, dtime.min),
                    bill_date=datetime.combine(end, dtime.min),
                    kwh_generated=3000, parse_status="parsed"))
        db.commit()
        tid, tkey = t.id, t.tenant_key

    transform_tenant_bills(tid)

    out = AO.array_owners_overview(authorization=f"Bearer {tkey}")
    assert len(out["arrays"]) == 1
    a = out["arrays"][0]
    assert a["lifetime"]["kwh"] > 0                  # not all zeros
    assert a["has_estimated"] is True                # honestly labeled
    assert a["health"]["status"] != "no_source"      # bills ARE a source
    assert out["totals"]["lifetime_kwh"] > 0
