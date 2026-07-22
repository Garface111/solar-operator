"""Continuous performance verification for Array Operator fleets.

Sunreport-parity layer: measured vs expected energy, PI/PR, persistence-aware
deviation classification, meter-primary boundary, automated monthly report pack.

Does NOT replace peer_analysis (relative health) or offtaker/NEPOOL reporting.
Does NOT clobber api.verification (workbook accuracy uploads).
"""
from __future__ import annotations

from .standards import METHOD_SUMMARY, REPORT_FOOTER, IEC_ALIGNMENT_NOTE
from .boundary import (
    BOUNDARY_METER,
    BOUNDARY_INVERTER,
    BOUNDARY_UNAVAILABLE,
    BOUNDARY_MIXED,
    select_measured_for_day,
    classify_series_boundary,
)
from .persistence import (
    DEFAULT_DEVIATION_THRESHOLD,
    classify_deviation,
    priority_score,
)
from .causes import infer_cause
from .availability import compute_availability
from .engine import (
    build_array_verification,
    build_month_verification,
    build_portfolio_verification,
)

__all__ = [
    "METHOD_SUMMARY",
    "REPORT_FOOTER",
    "IEC_ALIGNMENT_NOTE",
    "BOUNDARY_METER",
    "BOUNDARY_INVERTER",
    "BOUNDARY_UNAVAILABLE",
    "BOUNDARY_MIXED",
    "select_measured_for_day",
    "classify_series_boundary",
    "DEFAULT_DEVIATION_THRESHOLD",
    "classify_deviation",
    "priority_score",
    "infer_cause",
    "compute_availability",
    "build_array_verification",
    "build_portfolio_verification",
    "build_month_verification",
]
