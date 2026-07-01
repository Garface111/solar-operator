"""Live 'low vs peers' inverter alert (api/inverter_alert_sweep._live_low_inverters).

Ford's Waterford case: 11 inverters at ~101% of max, one at ~42% — the low one
must be flagged (it's producing, just far below its peers) so the morning email
includes it. Guards: only in daylight, only on fresh telemetry, only when the
cohort is genuinely producing, and only >15% below the peer median.
"""
from api.inverter_alert_sweep import _live_low_inverters

NP = 12.5  # kW nameplate (Fronius Primo 12.5)


def _inv(name, power_w, status="ok"):
    return {"inverter_id": name, "name": name, "status": status,
            "nameplate_kw": NP, "current_power_w": power_w}


def _tree(inverters, is_daylight=True, state="ok", age=0.1):
    return {"columns": [{
        "array_id": 1, "array_name": "Waterford 150kW", "is_daylight": is_daylight,
        "source_status": {"state": state, "age_hours": age},
        "inverters": inverters,
    }]}


def _peers(n, power_w=12600):     # ~101% of max
    return [_inv(f"Primo {i}", power_w) for i in range(1, n + 1)]


def test_flags_the_low_inverter():
    invs = _peers(11) + [_inv("Primo 12 (low)", 5200)]     # 5.2kW = 42% of max
    flagged = _live_low_inverters(_tree(invs))
    assert len(flagged) == 1
    assert flagged[0]["inv"]["name"] == "Primo 12 (low)"
    assert flagged[0]["reason"] == "live_low"
    assert 0.41 < flagged[0]["pct_of_max"] < 0.43
    assert flagged[0]["peer_median"] > 1.0


def test_healthy_spread_not_flagged():
    # all within ~101% ± a few % → nobody is >15% below the median
    invs = [_inv(f"Primo {i}", 12600 + (i % 3) * 150) for i in range(1, 13)]
    assert _live_low_inverters(_tree(invs)) == []


def test_borderline_14pct_below_not_flagged():
    # 14% below the median (median ~12600W → 12.5kW*.8664) — under the 15% bar
    invs = _peers(11) + [_inv("Primo 12", 12600 * 0.86)]
    assert _live_low_inverters(_tree(invs)) == []


def test_night_never_flags():
    invs = _peers(11) + [_inv("Primo 12", 5200)]
    assert _live_low_inverters(_tree(invs, is_daylight=False)) == []


def test_stale_source_never_flags():
    invs = _peers(11) + [_inv("Primo 12", 5200)]
    assert _live_low_inverters(_tree(invs, state="none", age=None)) == []
    assert _live_low_inverters(_tree(invs, age=9.0)) == []   # too old


def test_cohort_not_up_yet_not_flagged():
    # dawn: peers only at ~10% of max → don't judge the low one as a fault
    invs = [_inv(f"Primo {i}", int(NP * 1000 * 0.10)) for i in range(1, 12)] + [
        _inv("Primo 12", int(NP * 1000 * 0.02))]
    assert _live_low_inverters(_tree(invs)) == []


def test_non_ok_status_not_double_flagged():
    # a low inverter already 'dead'/'underperforming' isn't re-flagged live_low
    # (live_low is only for status=='ok'); the other channels own it.
    invs = _peers(11) + [_inv("Primo 12", 5200, status="underperforming")]
    assert _live_low_inverters(_tree(invs)) == []
