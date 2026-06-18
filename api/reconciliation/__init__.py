"""
api.reconciliation — production-vs-settlement audit layer.

This package turns Solar Operator from a credit-REPORT generator into a credit-
VARIANCE auditor. It does NOT mutate the database: every function reads bills +
daily_generation + inverter metadata and emits a ReconResult describing whether
metered production reconciles to utility settlement, and where it doesn't.

Public API:
    classify_array(db, array_id)            -> Classification
    reconcile_array(db, array_id, ws, we)   -> ReconResult

Spec: vt-solar-intel/host-meter-boundary-fix-spec.md
"""
from .classify import classify_array, Classification
from .reconcile import (
    reconcile_array,
    ReconResult,
    FEED_COMPLETENESS_FLOOR,
    LEAK_THRESHOLD_PCT,
    NAMEPLATE_CF_LOW,
    NAMEPLATE_CF_HIGH,
    FULL_COVERAGE_FLOOR,
)

__all__ = [
    "classify_array",
    "Classification",
    "reconcile_array",
    "ReconResult",
    "FEED_COMPLETENESS_FLOOR",
    "LEAK_THRESHOLD_PCT",
    "NAMEPLATE_CF_LOW",
    "NAMEPLATE_CF_HIGH",
    "FULL_COVERAGE_FLOOR",
]
