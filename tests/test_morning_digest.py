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
    # KPI labels
    assert "Arrays" in html
    assert "Inverters" in html
    assert "Need attention" in html
    # KPI values (2 arrays, 3 inverters, 0 attention)
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
    # array-level alert headline surfaced
    assert "Inverter fault — service drafted" in html


def test_attention_no_green_banner():
    html = digest.build_digest_html(_tenant(), _attention_tree())
    assert "All systems healthy" not in html


def test_no_data_array_is_honest():
    """An array with no daily history must never show a fabricated kWh."""
    html = digest.build_digest_html(_tenant(), _attention_tree())
    # the data-less "New Array" appears in the per-array table...
    assert "New Array" in html
    # ...and the digest says "no recent data" rather than inventing a number.
    assert "no recent data" in html


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
