"""Offline tests for performance verification report pack (P0).

No DB / network required for PDF + auditor CSV. Intervention uses a stub
session that yields no array → honest unavailable.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.perf_verification.auditor import (
    auditor_csv_bytes,
    auditor_zip_bytes,
    build_auditor_export,
)
from api.perf_verification.intervention import measure_recovery
from api.perf_verification.report_pack import (
    render_verification_html,
    render_verification_pdf,
)
from api.perf_verification.standards import REPORT_FOOTER


def _minimal_snapshot(*, available: bool = True) -> dict:
    if not available:
        return {
            "available": False,
            "tenant_id": "t-test",
            "period": "2026-06",
            "period_start": "2026-06-01",
            "period_end": "2026-06-30",
            "window_days": 30,
            "threshold": 0.05,
            "portfolio": {
                "array_count": 0,
                "skipped_count": 1,
                "actual_kwh": 0,
                "expected_matched_kwh": 0,
                "performance_index": None,
                "ratio_pct": None,
                "max_priority": 0.0,
            },
            "arrays": [],
            "skipped": [{"array_id": 1, "array_name": "Empty", "reason": "no_nameplate"}],
            "standards_note": "methods consistent with IEC 61724",
            "report_footer": REPORT_FOOTER,
            "method": {
                "measurement_boundary": "Meter preferred, else inverter.",
                "expected_energy": "POA × nameplate × PR.",
            },
        }
    return {
        "available": True,
        "tenant_id": "t-test",
        "period": "2026-06",
        "period_start": "2026-06-01",
        "period_end": "2026-06-30",
        "window_days": 30,
        "threshold": 0.05,
        "portfolio": {
            "array_count": 1,
            "skipped_count": 0,
            "actual_kwh": 900.0,
            "expected_matched_kwh": 1000.0,
            "performance_index": 0.9,
            "ratio_pct": 90,
            "max_priority": 12.5,
        },
        "arrays": [
            {
                "array_id": 42,
                "array_name": "Barn East",
                "performance_index": 0.9,
                "ratio_pct": 90,
                "boundary": "meter",
                "actual_kwh": 900.0,
                "expected_matched_kwh": 1000.0,
                "deviation": {
                    "label": "persistent",
                    "priority": 12.5,
                    "magnitude_mean": -0.1,
                    "duration_days": 7,
                },
                "cause": {
                    "cause": "soiling",
                    "confidence": "low",
                    "detail": "heuristic only",
                },
                "daily": [
                    {
                        "day": "2026-06-01",
                        "actual_kwh": 30.0,
                        "expected_kwh": 33.3,
                        "residual": -0.0991,
                        "boundary": "meter",
                    },
                    {
                        "day": "2026-06-02",
                        "actual_kwh": 28.0,
                        "expected_kwh": 32.0,
                        "residual": -0.125,
                        "boundary": "meter",
                    },
                ],
            }
        ],
        "skipped": [],
        "standards_note": "methods consistent with IEC 61724",
        "report_footer": REPORT_FOOTER,
        "method": {
            "measurement_boundary": "Meter preferred, else inverter.",
            "expected_energy": "POA × nameplate × PR.",
        },
    }


def test_render_verification_pdf_builds_from_synthetic_snapshot():
    pdf = render_verification_pdf(_minimal_snapshot(available=True))
    assert isinstance(pdf, (bytes, bytearray))
    assert len(pdf) > 500
    assert pdf[:4] == b"%PDF"


def test_render_verification_pdf_honest_empty():
    pdf = render_verification_pdf(_minimal_snapshot(available=False))
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 200


def test_render_verification_html_has_portfolio_pi():
    html, text = render_verification_html(
        _minimal_snapshot(available=True), company_name="Test Co"
    )
    assert "0.900" in html or "0.9" in html
    assert "Barn East" in html
    assert "Test Co" in text
    assert "soiling" in text or "soiling" in html


def test_auditor_csv_has_header():
    snap = _minimal_snapshot(available=True)
    export = build_auditor_export(snap)
    raw = auditor_csv_bytes(export["daily_rows"])
    assert isinstance(raw, (bytes, bytearray))
    text = raw.decode("utf-8")
    first = text.splitlines()[0]
    assert first == (
        "array_id,array_name,day,measured_kwh,expected_kwh,residual,pi_day,boundary"
    )
    assert "Barn East" in text
    assert "nan" not in text.lower()


def test_auditor_zip_contains_three_members():
    import io
    import zipfile

    zbytes = auditor_zip_bytes(_minimal_snapshot(available=True))
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        names = set(zf.namelist())
    assert "assumptions.json" in names
    assert "summary.json" in names
    assert "daily.csv" in names


def test_intervention_unavailable_without_data_no_crash():
    """No matching array → available=False, never raises."""
    db = MagicMock()
    # scalars().first() → None (array not found)
    db.execute.return_value.scalars.return_value.first.return_value = None
    tenant = SimpleNamespace(id="t-none", verification_deviation_threshold=None)

    result = measure_recovery(
        db,
        tenant,
        array_id=999,
        resolved_on=date(2026, 6, 15),
        window_days=14,
    )
    assert result["available"] is False
    assert result.get("reason")
    assert result.get("pi_before") is None
    assert result.get("pi_after") is None
    assert result.get("recovery_delta") is None
