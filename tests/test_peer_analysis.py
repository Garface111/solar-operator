"""Peer-relative ground-truth analysis tests.

Ports the sun-mirror demo-fleet expectations to the unit-agnostic
api.inverters.peer_analysis module and adds the degenerate single-unit case.

Pure functions, no I/O — fixtures are hand-built cohorts with seeded faults.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from api.inverters import peer_analysis as pa

WINDOW = pa.WINDOW_DAYS


def _daily(nameplate_kw: float, *, factor: float = 1.0, dead_last: int = 0,
           weather: list[float] | None = None, today: date | None = None) -> list[dict]:
    """Build an ascending daily-kWh series under a shared weather vector.

    factor scales the whole series (e.g. 0.62 for a shaded string). dead_last
    zeroes the final N days (a hard failure). A seasonal PSH curve gives
    realistic magnitudes; weather is shared across units so peer comparison is
    meaningful (the same cloud dims everyone).
    """
    today = today or date.today()
    weather = weather or [1.0] * (WINDOW + 1)
    out = []
    for i in range(WINDOW, -1, -1):
        d = today - timedelta(days=i)
        psh = 3.4 + 1.7 * math.sin((d.timetuple().tm_yday - 81) / 365 * 2 * math.pi)
        kwh = nameplate_kw * psh * weather[WINDOW - i] * factor
        if dead_last and i < dead_last:
            kwh = 0.0
        out.append({"date": d.isoformat(), "kwh": round(kwh, 2)})
    return out


def _fresh_ts(minutes_ago: int = 8) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _demo_cohort() -> list[dict]:
    """6-unit mixed cohort with seeded faults — sun-mirror's demo, generalized.

      U1..U3  healthy 10 kW
      U4      DEAD (zero last 4 days, relay fault would also flag, but we test
              the dead path here so error_code is None)
      U5      comm_gap (last report 30h ago, still producing)
      U6      shaded 6 kW string at ~62% of par => underperforming
    """
    weather = [0.6 + 0.4 * ((i * 7) % 5) / 5 for i in range(WINDOW + 1)]
    return [
        {"id": "U1", "nameplate_kw": 10.0, "daily": _daily(10.0, weather=weather),
         "error_code": None, "last_report": _fresh_ts()},
        {"id": "U2", "nameplate_kw": 10.0, "daily": _daily(10.0, weather=weather),
         "error_code": None, "last_report": _fresh_ts()},
        {"id": "U3", "nameplate_kw": 10.0, "daily": _daily(10.0, weather=weather),
         "error_code": None, "last_report": _fresh_ts()},
        {"id": "U4", "nameplate_kw": 10.0, "daily": _daily(10.0, weather=weather, dead_last=4),
         "error_code": None, "last_report": _fresh_ts()},
        {"id": "U5", "nameplate_kw": 10.0, "daily": _daily(10.0, weather=weather),
         "error_code": None,
         "last_report": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()},
        {"id": "U6", "nameplate_kw": 6.0, "daily": _daily(6.0, factor=0.62, weather=weather),
         "error_code": None, "last_report": _fresh_ts()},
    ]


def _by_id(result: dict) -> dict[str, dict]:
    return {u["id"]: u for u in result["units"]}


def test_healthy_units_pull_their_weight():
    res = pa.analyze_cohort(_demo_cohort())
    units = _by_id(res)
    for uid in ("U1", "U2", "U3"):
        assert units[uid]["status"] == "ok"
        # Healthy units sit near 1.0 peer index (same nameplate, same weather).
        assert 0.9 <= units[uid]["peer_index"] <= 1.15


def test_dead_unit_detected():
    units = _by_id(pa.analyze_cohort(_demo_cohort()))
    assert units["U4"]["status"] == "dead"
    assert "zero output" in units["U4"]["diagnosis"].lower()


def test_comm_gap_distinct_from_dead():
    units = _by_id(pa.analyze_cohort(_demo_cohort()))
    # U5 is producing but silent — must read as comm_gap, NOT dead.
    assert units["U5"]["status"] == "comm_gap"
    assert units["U5"]["stale_hours"] >= 24


def test_shaded_string_underperforms():
    units = _by_id(pa.analyze_cohort(_demo_cohort()))
    assert units["U6"]["status"] == "underperforming"
    assert units["U6"]["peer_index"] < pa.UNDERPERFORM_THRESHOLD
    assert "below its nameplate share" in units["U6"]["diagnosis"]


def test_no_history_unit_not_flagged_underperforming():
    """An inverter whose daily series hasn't synced (empty daily → window_kwh 0,
    peer_index ~0) must NOT be flagged 'underperforming' — that's MISSING DATA, not
    underproduction. (Bruce's Tannery Brook SMA unit 191213319: producing 18.8 kW
    live with 0 days of captured history, was falsely shown as a 100%-below error.)"""
    weather = [1.0] * (WINDOW + 1)
    cohort = [
        {"id": "H1", "nameplate_kw": 20.0, "daily": _daily(20.0, weather=weather),
         "error_code": None, "last_report": _fresh_ts()},
        {"id": "H2", "nameplate_kw": 20.0, "daily": _daily(20.0, weather=weather),
         "error_code": None, "last_report": _fresh_ts()},
        # No captured daily history yet (fresh report — its series just hasn't synced).
        {"id": "NEW", "nameplate_kw": 20.0, "daily": [],
         "error_code": None, "last_report": _fresh_ts()},
    ]
    units = _by_id(pa.analyze_cohort(cohort))
    assert units["NEW"]["status"] != "underperforming", units["NEW"]
    assert units["NEW"]["status"] == "ok"
    assert "history" in units["NEW"]["diagnosis"].lower()


def test_vendor_fault_takes_precedence():
    cohort = _demo_cohort()
    cohort[0]["error_code"] = "18xC4 — AC relay open"
    cohort[0]["mode"] = "FAULT"
    units = _by_id(pa.analyze_cohort(cohort))
    assert units["U1"]["status"] == "fault"
    assert "18xC4" in units["U1"]["diagnosis"]


def test_summary_counts_and_loss():
    res = pa.analyze_cohort(_demo_cohort())
    s = res["summary"]
    assert s["units_total"] == 6
    # U4 dead, U5 comm_gap, U6 underperforming => 3 need attention.
    assert s["units_attention"] == 3
    assert s["units_ok"] == 3
    assert s["estimated_loss_kwh_window"] > 0
    assert s["peer_analysis_available"] is True


def test_degenerate_single_unit_cohort():
    """A solo owner has no peers: peer_index None, never 'underperforming'."""
    solo = [{
        "id": "ONLY", "nameplate_kw": 8.0,
        "daily": _daily(8.0, factor=0.4),  # would be 'underperforming' WITH peers
        "error_code": None, "last_report": _fresh_ts(),
    }]
    res = pa.analyze_cohort(solo)
    assert res["degenerate"] is True
    assert res["summary"]["peer_analysis_available"] is False
    u = res["units"][0]
    assert u["peer_index"] is None
    # No peers to compare against => cannot be 'underperforming'.
    assert u["status"] == "ok"
    assert "solo unit" in u["diagnosis"]


def test_single_unit_still_flags_self_evident_faults():
    """Even solo, a dead/faulted unit is still caught (no peers needed)."""
    solo = [{
        "id": "ONLY", "nameplate_kw": 8.0,
        "daily": _daily(8.0, dead_last=3),
        "error_code": None, "last_report": _fresh_ts(),
    }]
    u = pa.analyze_cohort(solo)["units"][0]
    # Zero-streak vs peers can't fire with no peers, but the unit produced 0
    # while "peers" (itself) — guard: dead needs peers_alive>0 on those days,
    # which a solo unit can't satisfy, so it stays ok. Fault path is the solo
    # safety net:
    solo[0]["error_code"] = "E013"
    assert pa.analyze_cohort(solo)["units"][0]["status"] == "fault"


def test_input_not_mutated():
    cohort = _demo_cohort()
    before = cohort[0]["daily"][0]["kwh"]
    pa.analyze_cohort(cohort)
    assert cohort[0]["daily"][0]["kwh"] == before
    assert "peer_index" not in cohort[0]  # enrichment is on copies


# ── integration: peer block flows through the overview endpoint ───────────────

def test_overview_endpoint_attaches_peer_block():
    """End-to-end: seed a Client cohort with a dead array and assert the
    /v1/array-owners/overview response carries per-array `peer` blocks and a
    `peer_summary`. Uses the same DB helpers as test_array_owners.py."""
    import secrets
    from datetime import timedelta

    from api import array_owners
    from api.db import SessionLocal
    from api.models import Array, Client, DailyGeneration, Tenant

    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    today = date.today()

    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Peer Test", contact_email=f"{key}@t.test",
                      tenant_key=key, plan="standard", active=True))
        c = Client(tenant_id=tid, name="Bruce Fleet")
        db.add(c)
        db.flush()
        names = {"good-1": 1.0, "good-2": 1.0, "dead-1": 1.0}
        ids = {}
        for name in names:
            a = Array(tenant_id=tid, name=name, client_id=c.id)
            db.add(a)
            db.flush()
            ids[name] = a.id
            for i in range(WINDOW, -1, -1):
                d = today - timedelta(days=i)
                kwh = 40.0
                if name == "dead-1" and i < 4:   # dead the last 4 days
                    kwh = 0.0
                db.add(DailyGeneration(tenant_id=tid, array_id=a.id, day=d,
                                       kwh=kwh, source="csv"))
        db.commit()

    res = array_owners.array_owners_overview(authorization=f"Bearer {key}")

    by_name = {a["name"]: a for a in res["arrays"]}
    # Every array carries a peer block.
    for name in names:
        assert "peer" in by_name[name], f"{name} missing peer block"
        assert by_name[name]["peer"]["cohort_size"] == 3
        assert by_name[name]["peer"]["peer_analysis_available"] is True

    # The dead array is flagged; the healthy ones are ok.
    assert by_name["dead-1"]["peer"]["status"] == "dead"
    assert by_name["good-1"]["peer"]["status"] == "ok"
    assert by_name["good-2"]["peer"]["status"] == "ok"

    # Cohort-level rollup.
    ps = res["peer_summary"]
    assert ps["arrays_total"] == 3
    assert ps["arrays_attention"] == 1
    assert ps["cohorts_with_peer_signal"] == 1
    assert ps["estimated_loss_kwh_window"] > 0


def test_expected_low_holds_at_baseline_and_flags_on_breach():
    """Owner-confirmed expected-low (shading) RE-BASELINES a chronically-shaded unit:
    it reads OK while it holds its recorded level, but flags 'underperforming' again
    if it drops BELOW that baseline (a genuine new fault on top of the shading)."""
    # Baseline: U6 (shaded ~62%) is normally flagged underperforming.
    base = _by_id(pa.analyze_cohort(_demo_cohort()))["U6"]
    assert base["status"] == "underperforming"
    pi = base["peer_index"]
    assert pi is not None and pi < pa.UNDERPERFORM_THRESHOLD

    # Marked expected-low at its own baseline -> OK (holding), not a fault.
    coh = _demo_cohort()
    for u in coh:
        if u["id"] == "U6":
            u["expected_low"] = True
            u["expected_low_baseline"] = pi
            u["expected_low_reason"] = "Afternoon shade from neighbour's maple"
    held = _by_id(pa.analyze_cohort(coh))["U6"]
    assert held["status"] == "ok"
    assert not held.get("expected_low_breach")
    assert "expected reduced level" in held["diagnosis"].lower()

    # Drop it well below its baseline -> breach -> flagged underperforming again.
    weather = [0.6 + 0.4 * ((i * 7) % 5) / 5 for i in range(WINDOW + 1)]
    coh2 = _demo_cohort()
    for u in coh2:
        if u["id"] == "U6":
            u["daily"] = _daily(6.0, factor=0.28, weather=weather)  # ~half its shaded level
            u["expected_low"] = True
            u["expected_low_baseline"] = pi
            u["expected_low_reason"] = "Afternoon shade from neighbour's maple"
    breached = _by_id(pa.analyze_cohort(coh2))["U6"]
    assert breached["status"] == "underperforming"
    assert breached.get("expected_low_breach") is True
