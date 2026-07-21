"""Inverter alert-sweep detection tests.

Covers the two pure detection layers that decide what an operator is paged about:
  * _flagged_inverters  — 14-day/down statuses, with the comm_gap gate that keeps
                          extension capture-cadence gaps from spamming operators.
  * _live_dark_inverters — fast "dark right now while peers produce" anomaly that
                          mirrors the dashboard's isLiveAnomaly.

Pure functions over a fleet-tree dict — no DB, no network.
"""
from __future__ import annotations

from types import SimpleNamespace

from api import inverter_alert_sweep as sweep
from api.stripe_helpers import ao_gets_vendor_emails


def _inv(name, status, *, power=None, nameplate=20.0, peer_index=None, inverter_id=None,
         power_age_hours=0.1, power_estimated=False):
    # Defaults model a fresh, real per-device reading (the normal case). Override
    # power_age_hours (stale) or power_estimated (site-split fill) to exercise the
    # partial-capture gating that stopped Bruce's Chester false alert.
    return {
        "inverter_id": inverter_id or name,
        "name": name,
        "status": status,
        "current_power_w": power,
        "nameplate_kw": nameplate,
        "peer_index": peer_index,
        "power_age_hours": power_age_hours,
        "power_estimated": power_estimated,
    }


def _col(name, inverters, *, is_daylight=True, src_state="ok", age_hours=0.1,
         solar_elevation_deg=None, live_compare_ok=None):
    col = {
        "array_id": name,
        "array_name": name,
        "is_daylight": is_daylight,
        "source_status": {"state": src_state, "age_hours": age_hours,
                          "last_report": "2026-06-24T18:00:00+00:00"},
        "inverters": inverters,
    }
    if solar_elevation_deg is not None:
        col["solar_elevation_deg"] = solar_elevation_deg
    if live_compare_ok is not None:
        col["live_compare_ok"] = live_compare_ok
    return col


def _tree(*cols):
    return {"columns": list(cols)}


# ── live-dark detection ───────────────────────────────────────────────────────

def test_live_dark_flags_fresh_dark_while_peers_produce():
    col = _col("Londonderry", [
        _inv("A", "ok", power=5000),
        _inv("B", "ok", power=5000),
        _inv("C", "ok", power=0),   # dark while two peers produce
    ])
    flagged = sweep._live_dark_inverters(_tree(col))
    assert [f["inv"]["name"] for f in flagged] == ["C"]
    assert flagged[0]["reason"] == "live_dark"


def test_live_dark_skips_at_night():
    col = _col("Londonderry", [
        _inv("A", "ok", power=0), _inv("B", "ok", power=0), _inv("C", "ok", power=0),
    ], is_daylight=False)
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_skips_at_sunset_shoulder():
    # Sun still "up" for Sleeping UI (is_daylight True) but low — orientation /
    # shade makes one unit go dark while peers still produce. Must NOT page.
    # Ford 2026-07-21: dusk false positives on fleet "dark while neighbors produce".
    col = _col("Sunset", [
        _inv("A", "ok", power=5000),
        _inv("B", "ok", power=5000),
        _inv("C", "ok", power=0),
    ], is_daylight=True, solar_elevation_deg=5.0)
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_skips_when_live_compare_ok_false():
    col = _col("DuskFlag", [
        _inv("A", "ok", power=5000),
        _inv("B", "ok", power=5000),
        _inv("C", "ok", power=0),
    ], is_daylight=True, live_compare_ok=False)
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_flags_when_sun_high():
    # Explicit high sun must still page a genuine dark unit.
    col = _col("Noon", [
        _inv("A", "ok", power=5000),
        _inv("B", "ok", power=5000),
        _inv("C", "ok", power=0),
    ], is_daylight=True, solar_elevation_deg=40.0)
    assert [f["inv"]["name"] for f in sweep._live_dark_inverters(_tree(col))] == ["C"]


def test_live_low_skips_at_sunset_shoulder():
    col = _col("SunsetLow", [
        _inv("A", "ok", power=10000, nameplate=20.0),  # 50% of nameplate
        _inv("B", "ok", power=10000, nameplate=20.0),
        _inv("C", "ok", power=10000, nameplate=20.0),
        _inv("D", "ok", power=3000, nameplate=20.0),   # would be "low" at noon
    ], is_daylight=True, solar_elevation_deg=4.0)
    assert sweep._live_low_inverters(_tree(col)) == []


def test_live_dark_needs_two_lit_peers():
    # Only one producing peer → not enough signal to call the other dark.
    col = _col("Pair", [_inv("A", "ok", power=5000), _inv("B", "ok", power=0)])
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_skips_when_data_stale():
    # Fresh-enough peers but the array's telemetry is 5h old — we don't actually
    # know it's dark *now*, so don't page (extension vendor that hasn't captured).
    col = _col("Stale", [
        _inv("A", "ok", power=5000), _inv("B", "ok", power=5000), _inv("C", "ok", power=0),
    ], src_state="unpolled", age_hours=5.0)
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_skips_when_no_live_reading():
    # current_power_w is None (no live reading) → "stale", not a confirmed dark.
    col = _col("NoLive", [
        _inv("A", "ok", power=5000), _inv("B", "ok", power=5000),
        _inv("C", "ok", power=None),
    ])
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_ignores_non_ok_status():
    # A comm_gap/dead inverter is handled by the 14-day path, not live-dark.
    col = _col("Mixed", [
        _inv("A", "ok", power=5000), _inv("B", "ok", power=5000),
        _inv("C", "comm_gap", power=0),
    ])
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_respects_nameplate_floor():
    # 200 kW inverter: 1% floor = 2000 W. 1500 W is below par → dark.
    col = _col("Big", [
        _inv("A", "ok", power=80000, nameplate=200.0),
        _inv("B", "ok", power=80000, nameplate=200.0),
        _inv("C", "ok", power=1500, nameplate=200.0),
    ])
    assert [f["inv"]["name"] for f in sweep._live_dark_inverters(_tree(col))] == ["C"]


# ── partial-capture gating (Bruce's Chester false alert, 2026-07-03) ───────────

def test_live_dark_skips_inverter_with_stale_own_reading():
    # The exact false-positive shape: the ARRAY looks fresh (source age 0.1h, off
    # the units that DID capture), two peers produce, and C reads 0 — but C's OWN
    # per-device reading is 6h old (it just wasn't captured this cycle). We don't
    # actually know C is dark now, so it must NOT be paged. Pre-fix this alerted
    # 6 healthy Fronius inverters as "dark right now".
    col = _col("Chester", [
        _inv("A", "ok", power=6500),
        _inv("B", "ok", power=6500),
        _inv("C", "ok", power=0, power_age_hours=6.0),   # stale own reading
    ])
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_ignores_estimated_fill_as_producing_peer():
    # A site-total split fill (power_estimated) is fabricated per-inverter power —
    # it must not count as evidence a sibling is faulted. Here only A is a real
    # producer; B is an estimated fill; C reads a real fresh 0. One real peer < 2,
    # so nothing is called dark.
    col = _col("Partial", [
        _inv("A", "ok", power=6500),
        _inv("B", "ok", power=6500, power_estimated=True),  # fill, not real
        _inv("C", "ok", power=0),
    ])
    assert sweep._live_dark_inverters(_tree(col)) == []


def test_live_dark_still_flags_genuine_dark_on_complete_capture():
    # When every unit has a fresh, real per-device reading (a complete capture),
    # a genuinely dark inverter is still caught fast — the fix narrows to
    # trustworthy data, it doesn't disable the feature.
    col = _col("Complete", [
        _inv("A", "ok", power=6500),
        _inv("B", "ok", power=6400),
        _inv("C", "ok", power=6500),
        _inv("D", "ok", power=0),   # real fresh 0 while three real peers produce
    ])
    assert [f["inv"]["name"] for f in sweep._live_dark_inverters(_tree(col))] == ["D"]


def test_live_low_ignores_estimated_fill_as_peer():
    # live_low shares the gate: an estimated fill is neither judged nor used as a
    # peer. B (real, low) has only one real peer (A) after the fill C is dropped →
    # not enough to call B low.
    col = _col("LowPartial", [
        _inv("A", "ok", power=6500),
        _inv("B", "ok", power=2500),                        # real, low vs A
        _inv("C", "ok", power=6500, power_estimated=True),  # fill — dropped
    ])
    assert sweep._live_low_inverters(_tree(col)) == []


# ── comm_gap gate (the thing that makes on-by-default safe) ────────────────────

def test_comm_gap_suppressed_for_unpolled_capture_gap():
    # Extension vendor, owner hasn't captured in a day → every inverter reads
    # comm_gap but it's our cadence, not an outage. Must NOT page.
    col = _col("Fronius", [_inv("A", "comm_gap"), _inv("B", "comm_gap")],
               src_state="unpolled", age_hours=30.0)
    assert sweep._flagged_inverters(_tree(col), 50) == []


def test_comm_gap_kept_for_real_source_outage():
    # Real source-side outage (source_status 'stale' from the source's own ts).
    col = _col("RealOutage", [_inv("A", "comm_gap")], src_state="stale", age_hours=30.0)
    flagged = sweep._flagged_inverters(_tree(col), 50)
    assert [f["reason"] for f in flagged] == ["comm_gap"]


def test_dead_and_fault_flagged_on_real_source_signal():
    # A genuinely reporting (or genuinely source-side-silent) array: dead/fault
    # verdicts are backed by real evidence → page.
    for state in ("ok", "stale"):
        col = _col("Down", [_inv("A", "dead"), _inv("B", "fault")],
                   src_state=state, age_hours=1.0)
        reasons = sorted(f["reason"] for f in sweep._flagged_inverters(_tree(col), 50))
        assert reasons == ["dead", "fault"], state


def test_dead_and_fault_suppressed_on_capture_gap():
    # Ford 2026-07-04: never send a false report. On an 'unpolled' array the
    # dead/fault verdicts are frozen on an old capture — the inverter may have
    # been fixed days ago. No email until data flows again.
    col = _col("Down", [_inv("A", "dead"), _inv("B", "fault")],
               src_state="unpolled", age_hours=30.0)
    assert sweep._flagged_inverters(_tree(col), 50) == []


def test_underperforming_respects_threshold():
    col = _col("Under", [
        _inv("A", "underperforming", peer_index=0.40),  # below 50% → flag
        _inv("B", "underperforming", peer_index=0.80),  # above 50% → skip
    ])
    flagged = sweep._flagged_inverters(_tree(col), 50)
    assert [f["inv"]["name"] for f in flagged] == ["A"]


def test_underperforming_needs_fresh_data():
    # "Lagging its peers" is a claim about NOW — without fresh data (state 'ok')
    # it must not page, even on a real source-side outage ('stale': the source
    # stopped reporting, so there is no current output to compare).
    for state in ("stale", "unpolled", "none"):
        col = _col("Under", [_inv("A", "underperforming", peer_index=0.40)],
                   src_state=state, age_hours=30.0)
        assert sweep._flagged_inverters(_tree(col), 50) == [], state


# ── invoicing-only accounts don't get vendor-data emails ──────────────────────

def test_ao_gets_vendor_emails_predicate():
    AO = "array_operator"
    # Suppressed ONLY for explicit invoicing-only AO accounts.
    assert ao_gets_vendor_emails(AO, "invoicing") is False
    # Everyone else keeps getting them — incl. 'both' (has monitoring too) and the
    # not-yet-chosen (null) plan, so a legacy monitoring customer is never silenced.
    assert ao_gets_vendor_emails(AO, "monitoring") is True
    assert ao_gets_vendor_emails(AO, "both") is True
    assert ao_gets_vendor_emails(AO, None) is True
    assert ao_gets_vendor_emails(AO, "") is True
    # Non-AO (NEPOOL) tenants are never gated by AO plans.
    assert ao_gets_vendor_emails("nepool", "invoicing") is True
    assert ao_gets_vendor_emails(None, None) is True


def test_sweep_tenant_skips_invoicing_only_before_any_db_work():
    # db=None is safe: the invoicing-only gate returns 0 before build_fleet_tree.
    t = SimpleNamespace(
        id="t1", inverter_alerts_enabled=True, product="array_operator",
        billing_plan="invoicing", inverter_alert_email="x@y.com",
        contact_email="x@y.com", inverter_alert_grace_hours=12,
        inverter_alert_threshold_pct=50,
    )
    assert sweep.sweep_tenant(None, t) == 0


def test_via_digest_skips_separate_email(monkeypatch):
    """Owner folded alerts into the morning digest → no separate Resend page.

    We still stamp incident state (so open incidents are tracked), but
    notify._send_via_resend must not be called.
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace as NS

    now = datetime(2026, 7, 13, 15, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(sweep, "_now", lambda: now)

    # One long-flagged live-dark inverter past grace.
    col = _col("Site", [
        _inv("A", "ok", power=5000),
        _inv("B", "ok", power=5000),
        _inv("C", "ok", power=0, inverter_id="inv-c"),
    ])
    tree = _tree(col)
    monkeypatch.setattr(
        "api.inverter_fleet.build_fleet_tree",
        lambda db, tenant, stable_verdicts=False: tree,
    )

    sent = []
    monkeypatch.setattr(
        sweep.notify, "_send_via_resend",
        lambda **kw: sent.append(kw) or True,
    )

    # Fake SQLAlchemy session: empty InverterAlertState table on first load.
    # execute(...).scalars() is used both as an iterable (states dict-comp)
    # and via .all() elsewhere — support both.
    class _FakeScalars:
        def __iter__(self):
            return iter([])
        def all(self):
            return []
    class _FakeResult:
        def scalars(self):
            return _FakeScalars()
    class _FakeDB:
        def execute(self, *a, **k): return _FakeResult()
        def add(self, obj):
            # Simulate first_flagged long enough ago that grace has passed
            # (LIVE_DARK_GRACE_HOURS is short; set well in the past).
            if getattr(obj, "first_flagged_at", None) is not None:
                obj.first_flagged_at = now - timedelta(hours=3)
            self._added = getattr(self, "_added", []) + [obj]
        def delete(self, obj): pass
        def commit(self): pass

    t = NS(
        id="t-digest", inverter_alerts_enabled=True, product="array_operator",
        billing_plan="monitoring", inverter_alert_email="ops@example.com",
        contact_email="ops@example.com", inverter_alert_grace_hours=0,
        inverter_alert_threshold_pct=50, inverter_alerts_via_digest=True,
    )
    n = sweep.sweep_tenant(_FakeDB(), t)
    assert n == 1, "still counts the incident as handled"
    assert sent == [], "no separate alert email when via_digest is on"
