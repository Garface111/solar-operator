"""A unit that has always run at the same reduced share of its peers is built that
way, not failing. Real case: Tannery Brook 140kW #1 (SMA, 20 kW), 80-82% of its
neighbours every day since 2026-07-16; 7 'underperforming' tickets opened and
self-cleared in 2 months."""
from __future__ import annotations

from datetime import date, timedelta

from api.inverters.peer_analysis import analyze_cohort

START = date(2026, 6, 1)


def _days(start, values):
    return [{"date": (start + timedelta(days=i)).isoformat(), "kwh": v} for i, v in enumerate(values)]


def _unit(uid, hist, win):
    return {"id": uid, "nameplate_kw": 20.0, "error_code": None, "last_report": None,
            "history_daily": _days(START, hist), "daily": _days(START + timedelta(days=len(hist)), win)}


def _cohort(target_hist, target_win):
    n = len(target_hist)
    peers = [_unit(f"p{i}", [100.0 + i] * n, [100.0 + i] * 14) for i in range(5)]
    return peers + [_unit("t", target_hist, target_win)]


def _t(res):
    return next(u for u in res["units"] if u["id"] == "t")


def test_steady_structural_shortfall_is_ok_not_underperforming():
    t = _t(analyze_cohort(_cohort([81.0] * 60, [81.0] * 14)))
    assert t["status"] == "ok"
    assert t["chronic_low"]["baseline"] < 0.85 and "own usual level" in t["diagnosis"]


def test_drop_below_its_own_level_still_flags():
    t = _t(analyze_cohort(_cohort([81.0] * 60, [60.0] * 14)))
    assert t["status"] == "underperforming"


def test_erratic_history_is_not_a_baseline():
    # Tannery #2 pattern: mostly fine, with real 20-60% drops. Must stay flaggable.
    hist = ([88.0] * 6 + [30.0, 55.0, 25.0, 60.0]) * 6
    t = _t(analyze_cohort(_cohort(hist, [70.0] * 14)))
    assert t["status"] == "underperforming"


def test_newly_low_unit_with_healthy_history_flags():
    t = _t(analyze_cohort(_cohort([101.0] * 60, [75.0] * 14)))
    assert t["status"] == "underperforming"


def test_too_little_history_keeps_the_old_rule():
    t = _t(analyze_cohort(_cohort([81.0] * 10, [81.0] * 14)))
    assert t["status"] == "underperforming"
