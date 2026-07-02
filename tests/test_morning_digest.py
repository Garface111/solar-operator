"""Tests for the morning fleet-health digest (api/jobs/morning_fleet_digest).

build_digest_html is a pure function of (tenant, tree), so we can exercise the
full rendering from hand-built fake trees — no DB, no email, no network. We cover
the HEALTHY case (green "All systems healthy" banner, KPIs) and the ATTENTION
case (amber/red callout, flagged inverter named), plus the honesty rule that a
data-less array prints "—"/"no recent data", never a fabricated number.
"""
from types import SimpleNamespace

import api.jobs.morning_fleet_digest as digest


def _tenant():
    return SimpleNamespace(
        id="ten_test123",
        name="Test Owner",
        company_name="Sunny Acres Solar",
        operator_name="Pat Owner",
        contact_email="owner@example.test",
        product="array_operator",
    )


def _healthy_tree():
    """Two arrays, every inverter ok, recent kWh present."""
    return {
        "generated_at": "2026-06-17T12:00:00Z",
        "columns": [
            {
                "array_id": 1, "array_name": "South Field", "inverter_count": 2,
                "alert": {"level": "ok", "count": 0, "status": "ok", "headline": "All clear"},
                "inverters": [
                    {"inverter_id": 11, "name": "SF-1", "status": "ok"},
                    {"inverter_id": 12, "name": "SF-2", "status": "ok"},
                ],
                "daily": [{"date": "2026-06-15", "kwh": 40.0},
                          {"date": "2026-06-16", "kwh": 42.5}],
                "is_daylight": True,
            },
            {
                "array_id": 2, "array_name": "Barn Roof", "inverter_count": 1,
                "alert": {"level": "ok", "count": 0, "status": "ok", "headline": "All clear"},
                "inverters": [{"inverter_id": 21, "name": "BR-1", "status": "ok"}],
                "daily": [{"date": "2026-06-16", "kwh": 18.3}],
                "is_daylight": True,
            },
        ],
        "summary": {"arrays_total": 2, "inverters_total": 3,
                    "attention": 0, "is_daylight": True},
    }


def _attention_tree():
    """One faulted inverter (critical) + one underperformer, plus a no-data array."""
    return {
        "generated_at": "2026-06-17T12:00:00Z",
        "columns": [
            {
                "array_id": 1, "array_name": "South Field", "inverter_count": 2,
                "alert": {"level": "critical", "count": 1, "status": "fault",
                          "headline": "Inverter fault — service drafted"},
                "inverters": [
                    {"inverter_id": 11, "name": "SF-1", "status": "fault"},
                    {"inverter_id": 12, "name": "SF-2", "status": "ok"},
                ],
                "daily": [{"date": "2026-06-16", "kwh": 22.0}],
                "is_daylight": True,
            },
            {
                "array_id": 2, "array_name": "Barn Roof", "inverter_count": 1,
                "alert": {"level": "warn", "count": 1, "status": "underperforming",
                          "headline": "A money leak caught early"},
                "inverters": [
                    {"inverter_id": 21, "name": "BR-1", "status": "underperforming",
                     "peer_index": 0.4},
                ],
                "daily": [{"date": "2026-06-16", "kwh": 9.1}],
                "is_daylight": True,
            },
            {
                "array_id": 3, "array_name": "New Array", "inverter_count": 0,
                "alert": {"level": "ok", "count": 0, "status": "ok", "headline": "All clear"},
                "inverters": [],
                "daily": [],
                "is_daylight": True,
            },
        ],
        "summary": {"arrays_total": 3, "inverters_total": 3,
                    "attention": 2, "is_daylight": True},
    }


# ── healthy case ──────────────────────────────────────────────────────────────

def test_healthy_renders_valid_html_with_green_banner():
    html = digest.build_digest_html(_tenant(), _healthy_tree())
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html
    # green all-healthy banner
    assert "All systems healthy" in html
    # fleet name in header
    assert "Sunny Acres Solar" in html
    # no attention callout
    assert "need attention" not in html or "Need attention" in html  # KPI label allowed


def test_healthy_kpis_present():
    html = digest.build_digest_html(_tenant(), _healthy_tree())
    # health hero (mirrors the dashboard's "% fleet healthy" card). The label was
    # tightened to "Fleet health" in 3f67a98 ("clearer health %").
    assert "Fleet health" in html
    assert "100" in html                 # 100% healthy: 0 flagged of 3 inverters
    # stat strip (2 arrays, 3 inverters, 0 need a look)
    assert "arrays" in html
    assert "inverters" in html
    assert "need a look" in html
    assert ">2<" in html
    assert ">3<" in html
    assert ">0<" in html
    # per-array rows
    assert "South Field" in html
    assert "Barn Roof" in html
    # real recent kWh surfaced honestly
    assert "42.5 kWh" in html


def test_healthy_text_fallback():
    text = digest.build_digest_text(_tenant(), _healthy_tree())
    assert "All systems healthy" in text
    assert "Sunny Acres Solar" in text


# ── attention case ────────────────────────────────────────────────────────────

def test_attention_banner_and_callout():
    html = digest.build_digest_html(_tenant(), _attention_tree())
    assert html.lstrip().startswith("<!DOCTYPE html>")
    # amber/red attention banner (2 inverters need attention)
    assert "2 inverters need attention" in html
    # red color used somewhere (critical fault present)
    assert "#dc2626" in html or "#b91c1c" in html
    # amber color present (warn / underperformer)
    assert "#d97706" in html or "#b45309" in html


def test_attention_names_flagged_inverter():
    html = digest.build_digest_html(_tenant(), _attention_tree())
    # the faulted inverter is named with its plain-language phrase
    assert "SF-1" in html
    assert "reporting a fault" in html
    # the underperformer too
    assert "BR-1" in html
    assert "underperforming vs its neighbors" in html
    # the array holding the faulted inverter is identified (the digest names the
    # flagged inverter + its array in plain language, not the internal headline)
    assert "South Field" in html


def test_attention_no_green_banner():
    html = digest.build_digest_html(_tenant(), _attention_tree())
    assert "All systems healthy" not in html


def test_no_data_array_is_honest():
    """An array WITH inverters but NO daily history must show 'no full-day reading
    yet', never a fabricated kWh. (A 0-inverter array isn't a vendor array so the digest
    omits it entirely; and on an attention day only flagged arrays are listed — so we
    assert the honesty rule on a healthy day where every vendor array is shown.)"""
    tree = {
        "generated_at": "2026-06-17T12:00:00Z",
        "columns": [{
            "array_id": 9, "array_name": "Fresh Connect", "inverter_count": 1,
            "alert": {"level": "ok", "count": 0, "status": "ok", "headline": "All clear"},
            "inverters": [{"inverter_id": 91, "name": "FC-1", "status": "ok"}],
            "daily": [],   # freshly connected — no history yet
            "is_daylight": True,
        }],
        "summary": {"arrays_total": 1, "inverters_total": 1,
                    "attention": 0, "is_daylight": True},
    }
    html = digest.build_digest_html(_tenant(), tree)
    assert "Fresh Connect" in html                 # shown on a healthy day...
    assert "no full-day reading yet" in html       # ...honestly, never a fabricated kWh


def test_subject_lines():
    healthy = digest._subject(_tenant(), _healthy_tree())
    attn = digest._subject(_tenant(), _attention_tree())
    assert "all systems healthy" in healthy.lower()
    assert "attention" in attn.lower()
    assert "2" in attn


# ── empty fleet ───────────────────────────────────────────────────────────────

def test_empty_fleet_renders():
    tree = {"columns": [], "summary": {"arrays_total": 0, "inverters_total": 0,
                                       "attention": 0, "is_daylight": False}}
    html = digest.build_digest_html(_tenant(), tree)
    assert html.lstrip().startswith("<!DOCTYPE html>")
    # healthy banner (attention==0) and a no-arrays nudge
    assert "All systems healthy" in html
    assert "No arrays are connected yet" in html


# ── look-back-one-day + data-honesty (Bruce's real feedback) ──────────────────
from datetime import date, timedelta


def _iso_days_ago(n: int) -> str:
    base = date.fromisoformat(digest._local_today_iso())
    return (base - timedelta(days=n)).isoformat()


def _series(days, vals):
    return [{"date": d, "kwh": v} for d, v in zip(days, vals)]


def test_single_day_laggard_flagged():
    """A unit that ran ~50% of its cohort on the LAST FULL DAY is flagged even
    though its per-inverter status is 'ok' (14-day fine) — the look-back-one-day
    check Bruce asked for."""
    d = [_iso_days_ago(i) for i in (5, 4, 3, 2, 1)]
    invs = [
        {"name": "Primo-1", "status": "ok", "nameplate_kw": 10, "daily": _series(d, [50, 50, 50, 50, 50])},
        {"name": "Primo-2", "status": "ok", "nameplate_kw": 10, "daily": _series(d, [50, 50, 50, 50, 50])},
        {"name": "Primo-3", "status": "ok", "nameplate_kw": 10, "daily": _series(d, [50, 50, 50, 50, 25])},
    ]
    tree = {"columns": [{"array_id": 1, "array_name": "West Field", "inverter_count": 3,
                         "alert": {"level": "ok", "count": 0}, "inverters": invs,
                         "daily": _series(d, [150, 150, 150, 150, 125])}],
            "summary": {"attention": 0}}
    lags = digest._single_day_laggards(tree["columns"])
    assert [l["name"] for l in lags] == ["Primo-3"]
    html = digest.build_digest_html(_tenant(), tree)
    assert "Primo-3" in html
    assert "of its neighbors" in html
    assert digest._subject(_tenant(), tree) == "⚠️ Sunny Acres Solar: 1 inverter needs attention"


def test_weather_day_not_flagged():
    """The whole array uniformly low on a cloudy day → NO single-unit laggard
    (weather moves every unit together; only a divergent unit should flag)."""
    d = [_iso_days_ago(i) for i in (5, 4, 3, 2, 1)]
    invs = [{"name": f"P{i}", "status": "ok", "nameplate_kw": 10, "daily": _series(d, [50, 50, 50, 50, 25])}
            for i in range(3)]
    cols = [{"array_id": 1, "array_name": "Cloudy", "inverter_count": 3,
             "alert": {"level": "ok", "count": 0}, "inverters": invs,
             "daily": _series(d, [150, 150, 150, 150, 75])}]
    assert digest._single_day_laggards(cols) == []


def test_partial_capture_skipped():
    """A near-zero trailing day (interrupted extension capture, e.g. 0.1 kWh) is
    NOT reported as the last full day — we land on the prior complete day."""
    d = [_iso_days_ago(i) for i in (5, 4, 3, 2, 1)]
    col = {"array_id": 1, "array_name": "Waterford", "inverter_count": 1,
           "alert": {"level": "ok", "count": 0}, "inverters": [],
           "daily": _series(d, [100, 100, 100, 100, 0.1])}
    assert digest._recent_kwh(col) == 100
    assert digest._recent_day(col) == d[3]


def test_stale_data_note():
    """When the freshest full day is older than yesterday (extension hasn't
    captured), the header shows that real day + an honest staleness note."""
    old = _iso_days_ago(4)
    col = {"array_id": 1, "array_name": "OldData", "inverter_count": 2,
           "alert": {"level": "ok", "count": 0},
           "inverters": [{"name": "x", "status": "ok", "nameplate_kw": 10, "daily": [{"date": old, "kwh": 50}]},
                         {"name": "y", "status": "ok", "nameplate_kw": 10, "daily": [{"date": old, "kwh": 50}]}],
           "daily": [{"date": old, "kwh": 100}]}
    iso, label, stale = digest._fleet_reference_day([col])
    assert iso == old and stale is True
    html = digest.build_digest_html(_tenant(), {"columns": [col], "summary": {"attention": 0}})
    assert "refresh their readings" in html   # the staleness note rendered


def test_mixed_fleet_stale_not_masked_by_fresh_array():
    """A fresh array (SolarEdge, yesterday) must NOT mask a stale one (Fronius, days
    behind): the fleet still reads stale so the note fires."""
    fresh_day = _iso_days_ago(1)
    old_day = _iso_days_ago(3)
    fresh = {"array_id": 1, "array_name": "SolarEdge Site", "inverter_count": 1,
             "alert": {"level": "ok", "count": 0}, "inverters": [],
             "daily": [{"date": _iso_days_ago(2), "kwh": 100}, {"date": fresh_day, "kwh": 100}]}
    stale_arr = {"array_id": 2, "array_name": "Fronius Site", "inverter_count": 1,
                 "alert": {"level": "ok", "count": 0}, "inverters": [],
                 "daily": [{"date": _iso_days_ago(4), "kwh": 100}, {"date": old_day, "kwh": 100}]}
    iso, label, stale = digest._fleet_reference_day([fresh, stale_arr])
    assert iso == fresh_day     # header shows the freshest day...
    assert stale is True        # ...but staleness is NOT masked by it


def test_nonreporting_inverter_flagged():
    """A unit with NO data while its cohort produces (the dead Tannery #7: empty
    series, window_kwh 0, status 'ok') is flagged — the peer verdict couldn't judge
    it, but its producing siblings make the silence obvious."""
    d = [_iso_days_ago(i) for i in (3, 2, 1)]
    invs = [{"name": n, "status": "ok", "nameplate_kw": 20, "window_kwh": 300,
             "peak_kwh": 120, "daily": _series(d, [100, 100, 100])} for n in ("A", "B", "C")]
    invs.append({"name": "Dead-7", "status": "ok", "nameplate_kw": 20,
                 "window_kwh": 0, "peak_kwh": None, "daily": []})
    cols = [{"array_id": 1, "array_name": "Tannery", "inverter_count": 4,
             "alert": {"level": "ok", "count": 0}, "inverters": invs,
             "daily": _series(d, [300, 300, 300])}]
    flags = digest._nonreporting_inverters(cols)
    assert [f["name"] for f in flags] == ["Dead-7"]
    assert "isn't reporting" in flags[0]["phrase"]
    assert "Dead-7" in digest.build_digest_html(_tenant(), {"columns": cols, "summary": {"attention": 0}})


def test_recently_dead_inverter_caught_behind_overcast_day():
    """Paul's case: inverter (54) died 2 days ago (0.1 kWh vs ~100 for peers), but the
    LAST full day was heavy overcast (whole array ~5 kWh). Judging only the last day
    skips it as weather — we must scan back to the last producing day and flag the
    dead unit. Its healthy peers, which tracked the cohort, are NOT flagged."""
    d = [_iso_days_ago(i) for i in (3, 2, 1)]  # d[2] = yesterday (overcast)
    def inv(name, vals, peak=110):
        return {"name": name, "status": "ok", "nameplate_kw": 12.5, "peak_kwh": peak,
                "window_kwh": sum(vals), "daily": _series(d, vals)}
    invs = [inv("A", [100, 100, 5]), inv("B", [100, 100, 5]),
            inv("Dead-54", [100, 0.1, 0.05])]  # died on d[1]; overcast d[2]
    cols = [{"array_id": 1, "array_name": "Danville", "inverter_count": 3,
             "alert": {"level": "ok", "count": 0}, "inverters": invs,
             "daily": _series(d, [300, 200, 10])}]
    flags = digest._single_day_laggards(cols)
    names = [f["name"] for f in flags]
    assert "Dead-54" in names and "A" not in names and "B" not in names
    f = next(x for x in flags if x["name"] == "Dead-54")
    assert f["status"] == "dead"
    assert "stopped" in f["phrase"] or "almost nothing" in f["phrase"]
    # surfaces in the digest attention count
    assert "Dead-54" in digest.build_digest_html(_tenant(), {"columns": cols, "summary": {"attention": 0}})


def test_whole_array_gap_not_flagged():
    """When only a minority is producing (an array-wide capture gap), we do NOT flag
    every silent unit — the staleness note covers that instead."""
    d = [_iso_days_ago(i) for i in (3, 2, 1)]
    invs = [{"name": "A", "status": "ok", "nameplate_kw": 20, "window_kwh": 300,
             "peak_kwh": 120, "daily": _series(d, [100, 100, 100])}]
    invs += [{"name": f"X{i}", "status": "ok", "nameplate_kw": 20, "window_kwh": 0,
              "peak_kwh": None, "daily": []} for i in range(3)]
    cols = [{"array_id": 1, "array_name": "Gap", "inverter_count": 4,
             "alert": {"level": "ok", "count": 0}, "inverters": invs,
             "daily": _series(d, [100, 100, 100])}]
    assert digest._nonreporting_inverters(cols) == []


def test_stopped_reporting_flagged():
    """A unit that WAS reporting but went silent days ago while its siblings keep
    reporting is flagged."""
    d = [_iso_days_ago(i) for i in (5, 4, 3, 2, 1)]
    invs = [{"name": n, "status": "ok", "nameplate_kw": 20, "window_kwh": 500,
             "peak_kwh": 110, "daily": _series(d, [100, 100, 100, 100, 100])} for n in ("A", "B")]
    invs.append({"name": "C", "status": "ok", "nameplate_kw": 20, "window_kwh": 200,
                 "peak_kwh": 110, "daily": _series(d[:2], [100, 100])})  # last reading 4 days ago
    cols = [{"array_id": 1, "array_name": "Arr", "inverter_count": 3,
             "alert": {"level": "ok", "count": 0}, "inverters": invs,
             "daily": _series(d, [300, 300, 300, 200, 200])}]
    flags = digest._nonreporting_inverters(cols)
    assert any(f["name"] == "C" and "stopped reporting" in f["phrase"] for f in flags)
