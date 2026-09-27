"""A catch-up burst (zeros, then one impossible day) is a reporting gap, not a dead unit.

Real case, GMCS Tinker Hall Inverter 15 (36 kW): exact 0 kWh every day
2026-07-09..08-05, then 6,102 kWh on 08-06 (about 35 days of its share at once),
then 215 kWh. It was flagged "dead" and the owner was emailed on day 7.
"""
from __future__ import annotations

from datetime import date, timedelta

from api.inverters.peer_analysis import analyze_cohort


def _days(start: date, values: list[float]) -> list[dict]:
    return [{"date": (start + timedelta(days=i)).isoformat(), "kwh": v} for i, v in enumerate(values)]


START = date(2026, 7, 27)


def _peers(n=3, kwh=190.0, days=14):
    return [
        {"id": f"peer{i}", "nameplate_kw": 36.0, "daily": _days(START, [kwh] * days), "error_code": None, "last_report": None}
        for i in range(n)
    ]


def _unit(values):
    return {"id": "inv15", "nameplate_kw": 36.0, "daily": _days(START, values), "error_code": None, "last_report": None}


def _status(res, uid="inv15"):
    return next(u for u in res["units"] if u["id"] == uid)


def test_zeros_then_burst_is_not_dead_and_burst_is_not_output():
    # 10 zero days, the 6,102 kWh catch-up, then normal days again.
    u = _unit([0.0] * 10 + [6102.0, 190.0, 191.0, 189.0])
    res = analyze_cohort(_peers() + [u])
    inv = _status(res)
    assert inv["status"] == "ok"
    assert inv["catchup_burst"] == {"date": "2026-08-06", "kwh": 6102.0, "zero_days_before": 10}
    # The burst must not count as production: its peer_index stays near 1, not ~4.
    assert inv["peer_index"] is not None and inv["peer_index"] < 1.5
    assert inv["window_kwh"] < 1000


def test_zeros_again_after_a_burst_is_comm_gap_not_dead():
    # Burst, then it goes back to reporting 0 (what Inverter 15 did after 08-08).
    u = _unit([0.0] * 3 + [900.0] + [0.0] * 10)
    inv = _status(analyze_cohort(_peers() + [u]))
    assert inv["status"] == "comm_gap"
    assert "producing but not reporting" in inv["diagnosis"]


def test_plain_zeros_without_a_burst_are_still_dead():
    u = _unit([190.0] * 4 + [0.0] * 10)
    inv = _status(analyze_cohort(_peers() + [u]))
    assert inv["status"] == "dead"
    assert "catchup_burst" not in inv


def test_a_big_but_possible_day_is_not_a_burst():
    # 300 kWh on 36 kW = 8.3 kWh/kW: bright, possible, under the 9 kWh/kW limit.
    u = _unit([190.0] * 13 + [300.0])
    inv = _status(analyze_cohort(_peers() + [u]))
    assert "catchup_burst" not in inv
    assert inv["status"] == "ok"


def test_inferred_nameplate_never_triggers_burst_logic():
    u = {"id": "inv15", "nameplate_kw": None, "daily": _days(START, [0.0] * 10 + [6102.0, 190.0, 191.0, 189.0]),
         "error_code": None, "last_report": None}
    inv = _status(analyze_cohort(_peers() + [u]))
    assert "catchup_burst" not in inv
