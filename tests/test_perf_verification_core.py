"""Pure-module unit tests for api.perf_verification (no DB, no network).

Covers boundary selection, series boundary badge, deviation classification,
priority scoring, cause inference, availability KPIs, and standards smoke.
"""
from __future__ import annotations

from datetime import date, timedelta

from api.perf_verification.boundary import (
    BOUNDARY_INVERTER,
    BOUNDARY_METER,
    BOUNDARY_MIXED,
    BOUNDARY_UNAVAILABLE,
    classify_series_boundary,
    select_measured_for_day,
)
from api.perf_verification.persistence import (
    classify_deviation,
    priority_score,
)
from api.perf_verification.causes import infer_cause
from api.perf_verification.availability import compute_availability
from api.perf_verification import standards


# ── helpers ──────────────────────────────────────────────────────────────────

def _day_series(residuals, start=date(2026, 6, 1)):
    """Build residual series for classify_deviation: one entry per day."""
    out = []
    for i, r in enumerate(residuals):
        out.append({"day": start + timedelta(days=i), "residual": r})
    return out


# ── 1–3 boundary: select_measured_for_day ────────────────────────────────────

def test_select_measured_meter_beats_inverter():
    """Meter (utility-real) sources beat inverter when both present."""
    rows = [
        {"kwh": 40.0, "source": "solaredge"},
        {"kwh": 38.5, "source": "gmp_api"},
        {"kwh": 1.0, "source": "extension_pull"},
    ]
    got = select_measured_for_day(rows)
    assert got["boundary"] == BOUNDARY_METER
    assert got["kwh"] == 38.5
    assert "gmp_api" in got["sources"]
    assert got["used_estimate"] is False
    # Inverter sources must not leak into the chosen sources list
    assert "solaredge" not in got["sources"]


def test_select_measured_ignores_bill_prorate_and_utility_meter():
    """Estimates (bill_prorate, utility_meter) never count as measured."""
    # Estimates alone → unavailable
    alone = select_measured_for_day([
        {"kwh": 100.0, "source": "bill_prorate"},
        {"kwh": 90.0, "source": "utility_meter"},
    ])
    assert alone["boundary"] == BOUNDARY_UNAVAILABLE
    assert alone["kwh"] is None
    assert alone["sources"] == []

    # Estimates present with inverter → still inverter (estimates ignored)
    with_inv = select_measured_for_day([
        {"kwh": 100.0, "source": "bill_prorate"},
        {"kwh": 42.0, "source": "enphase"},
        {"kwh": 90.0, "source": "utility_meter"},
    ])
    assert with_inv["boundary"] == BOUNDARY_INVERTER
    assert with_inv["kwh"] == 42.0
    assert "bill_prorate" not in with_inv["sources"]
    assert "utility_meter" not in with_inv["sources"]


def test_select_measured_unavailable_when_empty():
    empty = select_measured_for_day([])
    assert empty["boundary"] == BOUNDARY_UNAVAILABLE
    assert empty["kwh"] is None
    assert empty["sources"] == []
    assert empty["used_estimate"] is False

    none_rows = select_measured_for_day(None)  # type: ignore[arg-type]
    assert none_rows["boundary"] == BOUNDARY_UNAVAILABLE


# ── 4 series boundary ────────────────────────────────────────────────────────

def test_classify_series_boundary_mixed():
    """Series with both meter and inverter days → mixed."""
    assert classify_series_boundary(
        [BOUNDARY_METER, BOUNDARY_INVERTER, BOUNDARY_METER]
    ) == BOUNDARY_MIXED
    # unavailable days ignored when classifying mixed
    assert classify_series_boundary(
        [BOUNDARY_METER, BOUNDARY_UNAVAILABLE, BOUNDARY_INVERTER]
    ) == BOUNDARY_MIXED
    # pure cases
    assert classify_series_boundary([BOUNDARY_METER, BOUNDARY_METER]) == BOUNDARY_METER
    assert classify_series_boundary(
        [BOUNDARY_INVERTER, BOUNDARY_UNAVAILABLE]
    ) == BOUNDARY_INVERTER
    assert classify_series_boundary(
        [BOUNDARY_UNAVAILABLE, BOUNDARY_UNAVAILABLE]
    ) == BOUNDARY_UNAVAILABLE
    assert classify_series_boundary([]) == BOUNDARY_UNAVAILABLE


# ── 5–6 deviation classification ─────────────────────────────────────────────

def test_classify_deviation_sudden_after_healthy_baseline():
    """Last days crash after healthy baseline → sudden."""
    # 7 healthy days (~0 residual) then 2 crash days
    residuals = [0.02, -0.01, 0.0, 0.01, -0.02, -0.01, 0.0, -0.25, -0.30]
    got = classify_deviation(_day_series(residuals), threshold=0.05)
    assert got.label == "sudden"
    assert got.duration_days >= 1
    assert got.magnitude_mean is not None
    assert got.magnitude_mean < -0.05
    assert got.priority > 0
    assert "Sudden" in got.detail or "sudden" in got.detail.lower()


def test_classify_deviation_persistent_many_consecutive_under_days():
    """Many consecutive under days → persistent."""
    # 8 days well below -5%
    residuals = [-0.12, -0.15, -0.10, -0.18, -0.14, -0.11, -0.16, -0.13]
    got = classify_deviation(_day_series(residuals), threshold=0.05, persistent_days=5)
    assert got.label == "persistent"
    assert got.duration_days >= 5
    assert got.magnitude_mean is not None
    assert got.magnitude_mean < -0.05
    assert got.priority > 0


# ── 7 priority_score ─────────────────────────────────────────────────────────

def test_priority_score_duration_and_sudden_multiplier():
    """Priority increases with duration; sudden > non-sudden for same mag/duration."""
    mag = 0.20
    short = priority_score(mag, 3, sudden=False)
    long_ = priority_score(mag, 10, sudden=False)
    assert long_ > short
    assert short > 0

    base = priority_score(mag, 7, sudden=False)
    sudden = priority_score(mag, 7, sudden=True)
    assert sudden > base
    # sudden is 1.5× (clamped at 100)
    assert sudden == min(100.0, round(base * 1.5, 1)) or sudden >= base * 1.4

    assert priority_score(0.0, 5) == 0.0
    assert priority_score(0.1, 0) == 0.0
    assert priority_score(1.0, 100, sudden=True) <= 100.0


# ── 8–10 cause inference ─────────────────────────────────────────────────────

def test_infer_cause_electrical_on_fault_or_dead():
    """peer_status fault/dead (or fault_code) → electrical."""
    for status in ("fault", "dead"):
        got = infer_cause(
            deviation_label="persistent",
            residual_mean=-0.2,
            peer_status=status,
            measured_days=10,
            window_days=14,
        )
        assert got.cause == "electrical", status
        assert got.confidence in ("low", "medium")

    with_code = infer_cause(
        deviation_label="sudden",
        residual_mean=-0.3,
        fault_code="E-301",
        measured_days=10,
        window_days=14,
    )
    assert with_code.cause == "electrical"
    assert "E-301" in with_code.detail


def test_infer_cause_data_quality_when_few_measured_days():
    """Sparse measured days → data_quality (before physical causes)."""
    got = infer_cause(
        deviation_label="persistent",
        residual_mean=-0.2,
        peer_status="fault",  # would be electrical if data were sufficient
        measured_days=1,
        window_days=14,
    )
    assert got.cause == "data_quality"
    assert got.confidence == "medium"

    unavail = infer_cause(
        deviation_label="ok",
        residual_mean=None,
        measured_days=10,
        window_days=14,
        boundary="unavailable",
    )
    assert unavail.cause == "data_quality"


def test_infer_cause_shading_when_expected_low():
    """Site marked expected_low → shading."""
    got = infer_cause(
        deviation_label="persistent",
        residual_mean=-0.15,
        expected_low=True,
        measured_days=12,
        window_days=14,
        boundary="inverter",
    )
    assert got.cause == "shading"
    assert "shading" in got.detail.lower() or "expected-low" in got.detail.lower()


# ── 11–12 availability ───────────────────────────────────────────────────────

def test_compute_availability_no_measured_days_when_empty():
    got = compute_availability(window_days=14, daily=[])
    assert got.all_in_energy_kwh is None
    assert got.in_service_energy_kwh is None
    assert got.availability_pct is None
    assert got.reason == "no_measured_days"
    assert got.window_days == 14

    # rows with only null actuals also count as no measured
    nullish = compute_availability(
        window_days=7,
        daily=[{"actual_kwh": None, "expected_kwh": 10.0}],
    )
    assert nullish.reason == "no_measured_days"


def test_compute_availability_energy_proxy_when_no_status():
    """No inverter status → energy_proxy_no_status with all-in energy."""
    daily = [
        {"actual_kwh": 50.0, "expected_kwh": 55.0},
        {"actual_kwh": 48.0, "expected_kwh": 52.0},
        {"actual_kwh": 0.0, "expected_kwh": 50.0},  # weak downtime signal
        {"actual_kwh": 51.0, "expected_kwh": 53.0},
    ]
    got = compute_availability(window_days=4, daily=daily)
    assert got.reason == "energy_proxy_no_status"
    assert got.all_in_energy_kwh == 50.0 + 48.0 + 0.0 + 51.0
    assert got.in_service_energy_kwh == got.all_in_energy_kwh
    assert got.downtime_days == 1  # one zero day with expected > 1
    assert got.availability_pct is not None
    assert 0.0 <= got.availability_pct <= 100.0


# ── standards smoke ──────────────────────────────────────────────────────────

def test_standards_smoke():
    """Standards module exposes IEC-aligned method text without claiming certification."""
    assert "IEC 61724" in standards.IEC_ALIGNMENT_NOTE
    assert "certification" in standards.IEC_ALIGNMENT_NOTE.lower() or "certified" in standards.IEC_ALIGNMENT_NOTE.lower()
    # must not claim we are certified
    assert "certified to IEC" not in standards.IEC_ALIGNMENT_NOTE.lower()

    assert isinstance(standards.METHOD_SUMMARY, dict)
    assert "title" in standards.METHOD_SUMMARY
    assert "measurement_boundary" in standards.METHOD_SUMMARY
    assert "bill_prorate" in standards.METHOD_SUMMARY["measurement_boundary"]

    assert standards.REPORT_FOOTER
    assert "IEC 61724" in standards.REPORT_FOOTER
    assert standards.DEFAULT_DEVIATION_THRESHOLD == 0.05
    assert standards.DEFAULT_REPORT_WINDOW_DAYS == 30
