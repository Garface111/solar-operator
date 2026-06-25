"""Bruce's dawn false-positive fix: stable-mode peer analysis (complete days +
cohort-relative comm-gap) must clear the early-morning erroneous verdicts while
still catching genuine faults."""
from datetime import datetime, timezone, timedelta

from api.inverters import peer_analysis as pa


def _at(h):  # ISO ts h hours before the fixed 'now'
    return (NOW - timedelta(hours=h)).isoformat()


# Fixed reference: 06:00 ET = 10:00 UTC on 2026-06-25 (a dawn run).
NOW = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
TZ = "America/New_York"
DAYS = ["2026-06-23", "2026-06-24", "2026-06-25"]  # ...-25 is "today" (partial)


def _unit(uid, np, daily, last_report):
    return {"id": uid, "nameplate_kw": np, "daily": daily, "error_code": None,
            "last_report": last_report}


def test_partial_dawn_day_does_not_false_flag_underperforming():
    # Three identical healthy 10kW inverters. Yesterday all made 50 kWh. TODAY is
    # a lopsided dawn: captures landed unevenly (A barely, others a bit more).
    units = [
        _unit("A", 10, [{"date": "2026-06-24", "kwh": 50.0}, {"date": "2026-06-25", "kwh": 0.2}], _at(1)),
        _unit("B", 10, [{"date": "2026-06-24", "kwh": 50.0}, {"date": "2026-06-25", "kwh": 3.0}], _at(1)),
        _unit("C", 10, [{"date": "2026-06-24", "kwh": 50.0}, {"date": "2026-06-25", "kwh": 4.0}], _at(1)),
    ]
    # LIVE mode (includes today's partial) — A's tiny today share can read as a deficit.
    live = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW)["units"]}
    # STABLE mode — today dropped; everyone made 50 kWh yesterday → all healthy.
    stable = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW,
                                                     complete_days_only=True, tz_name=TZ)["units"]}
    assert all(stable[k]["status"] == "ok" for k in "ABC"), \
        {k: stable[k]["status"] for k in "ABC"}


def test_short_history_today_dominates_then_clears():
    # Newly-connected: only yesterday + today's dawn partial. One inverter's dawn
    # capture is low -> LIVE flags underperforming; STABLE (drop today) sees an
    # equal yesterday -> ok.
    units = [
        _unit("A", 10, [{"date": "2026-06-24", "kwh": 48.0}, {"date": "2026-06-25", "kwh": 0.1}], _at(1)),
        _unit("B", 10, [{"date": "2026-06-24", "kwh": 49.0}, {"date": "2026-06-25", "kwh": 18.0}], _at(1)),
        _unit("C", 10, [{"date": "2026-06-24", "kwh": 50.0}, {"date": "2026-06-25", "kwh": 19.0}], _at(1)),
    ]
    live = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW)["units"]}
    stable = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW,
                                                    complete_days_only=True, tz_name=TZ)["units"]}
    assert live["A"]["status"] == "underperforming"   # the bug Bruce saw at dawn
    assert stable["A"]["status"] == "ok"               # fixed on settled data


def test_overnight_staleness_is_not_comm_gap_but_a_dead_gateway_is():
    # Everyone's last capture was ~30h ago (overnight, panels asleep). LIVE flags
    # the whole array comm_gap (>24h); STABLE (cohort-relative) flags none.
    daily = [{"date": "2026-06-23", "kwh": 50.0}, {"date": "2026-06-24", "kwh": 50.0}]
    units = [_unit(x, 10, list(daily), _at(30)) for x in "ABC"]
    live = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW)["units"]}
    stable = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW,
                                                    complete_days_only=True, tz_name=TZ)["units"]}
    assert all(live[k]["status"] == "comm_gap" for k in "ABC")     # the dawn false alarm
    assert all(stable[k]["status"] == "ok" for k in "ABC")         # fixed

    # Now one device's gateway truly died: it's 60h stale while peers are fresh (5h).
    units2 = [
        _unit("A", 10, list(daily), _at(5)),
        _unit("B", 10, list(daily), _at(5)),
        _unit("C", 10, list(daily), _at(60)),
    ]
    stable2 = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units2], now=NOW,
                                                     complete_days_only=True, tz_name=TZ)["units"]}
    assert stable2["C"]["status"] == "comm_gap"   # genuine dropout still caught
    assert stable2["A"]["status"] == "ok"


def test_capture_gaps_do_not_fake_underperformance():
    # The real bug on Bruce's fleet: capture gaps store missing days as 0, so an
    # inverter that simply captured FEWER days has a low window sum and reads
    # "underperforming" though its output on the days it reported matches peers.
    full = [{"date": f"2026-06-{d:02d}", "kwh": 32.0} for d in range(10, 24)]   # 14 complete days
    sparse = [{"date": f"2026-06-{d:02d}", "kwh": (32.0 if d in (12, 16, 20, 23) else 0.0)}
              for d in range(10, 24)]                                            # only 4 real days
    units = [
        _unit("A", 7.6, [dict(x) for x in full], _at(2)),
        _unit("B", 7.6, [dict(x) for x in full], _at(2)),
        _unit("C", 7.6, [dict(x) for x in sparse], _at(2)),
    ]
    live = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW)["units"]}
    stable = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW,
                                                    complete_days_only=True, tz_name=TZ)["units"]}
    assert live["C"]["status"] == "underperforming"      # the false positive (raw window sum)
    assert stable["C"]["status"] == "ok", (stable["C"]["status"], stable["C"]["peer_index"])


def test_genuine_everyday_deficit_still_flags_in_stable_mode():
    # Reports every day but at ~62% of peers -> a real deficit, must still flag.
    full = [{"date": f"2026-06-{d:02d}", "kwh": 32.0} for d in range(10, 24)]
    weak = [{"date": f"2026-06-{d:02d}", "kwh": 20.0} for d in range(10, 24)]
    units = [
        _unit("A", 7.6, [dict(x) for x in full], _at(2)),
        _unit("B", 7.6, [dict(x) for x in full], _at(2)),
        _unit("C", 7.6, [dict(x) for x in weak], _at(2)),
    ]
    stable = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW,
                                                    complete_days_only=True, tz_name=TZ)["units"]}
    assert stable["C"]["status"] == "underperforming", (stable["C"]["status"], stable["C"]["peer_index"])


def test_genuine_dead_still_flags_in_stable_mode():
    # C produced zero for two COMPLETE days while peers produced -> dead, even
    # after dropping today.
    units = [
        _unit("A", 10, [{"date": "2026-06-23", "kwh": 50.0}, {"date": "2026-06-24", "kwh": 50.0}], _at(2)),
        _unit("B", 10, [{"date": "2026-06-23", "kwh": 50.0}, {"date": "2026-06-24", "kwh": 50.0}], _at(2)),
        _unit("C", 10, [{"date": "2026-06-23", "kwh": 0.0}, {"date": "2026-06-24", "kwh": 0.0}], _at(2)),
    ]
    stable = {u["id"]: u for u in pa.analyze_cohort([dict(x) for x in units], now=NOW,
                                                    complete_days_only=True, tz_name=TZ)["units"]}
    assert stable["C"]["status"] == "dead", stable["C"]["status"]
