"""Auditor export for performance verification (CSV + assumptions ZIP).

Ships with P0: no invented numbers — only fields present on the snapshot.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any


def build_auditor_export(snapshot: dict) -> dict:
    """Build auditor payload: assumptions, daily_rows, summary.

    Keys required by contract:
      assumptions (dict), daily_rows (list[dict]), summary (dict)
    """
    method = snapshot.get("method") or {}
    assumptions: dict[str, Any] = {
        "standards_note": snapshot.get("standards_note"),
        "report_footer": snapshot.get("report_footer"),
        "threshold": snapshot.get("threshold"),
        "measurement_boundary": method.get("measurement_boundary"),
        "expected_energy": method.get("expected_energy"),
        "kpis": method.get("kpis"),
        "windows": method.get("windows"),
        "honesty": method.get("honesty"),
        "alignment": method.get("alignment") or snapshot.get("standards_note"),
        "period": snapshot.get("period"),
        "period_start": snapshot.get("period_start") or snapshot.get("window_start"),
        "period_end": snapshot.get("period_end") or snapshot.get("window_end"),
        "window_days": snapshot.get("window_days"),
        "tenant_id": snapshot.get("tenant_id"),
    }

    daily_rows: list[dict] = []
    for arr in snapshot.get("arrays") or []:
        array_id = arr.get("array_id")
        array_name = arr.get("array_name")
        for d in arr.get("daily") or []:
            measured = d.get("actual_kwh")
            expected = d.get("expected_kwh")
            residual = d.get("residual")
            if residual is None and measured is not None and expected:
                try:
                    exp_f = float(expected)
                    if exp_f > 0:
                        residual = (float(measured) - exp_f) / exp_f
                except (TypeError, ValueError):
                    residual = None
            pi_day = None
            if measured is not None and expected:
                try:
                    exp_f = float(expected)
                    if exp_f > 0:
                        pi_day = round(float(measured) / exp_f, 4)
                except (TypeError, ValueError):
                    pi_day = None
            daily_rows.append({
                "array_id": array_id,
                "array_name": array_name,
                "day": d.get("day"),
                "measured_kwh": measured,
                "expected_kwh": expected,
                "residual": residual,
                "pi_day": pi_day,
                "boundary": d.get("boundary"),
            })

    portfolio = snapshot.get("portfolio") or {}
    summary: dict[str, Any] = {
        "available": bool(snapshot.get("available")),
        "reason": snapshot.get("reason"),
        "period": snapshot.get("period"),
        "period_start": snapshot.get("period_start") or snapshot.get("window_start"),
        "period_end": snapshot.get("period_end") or snapshot.get("window_end"),
        "window_days": snapshot.get("window_days"),
        "threshold": snapshot.get("threshold"),
        "tenant_id": snapshot.get("tenant_id"),
        "portfolio": portfolio,
        "array_count": portfolio.get("array_count"),
        "skipped_count": portfolio.get("skipped_count"),
        "skipped": snapshot.get("skipped") or [],
        "arrays": [
            {
                "array_id": a.get("array_id"),
                "array_name": a.get("array_name"),
                "performance_index": a.get("performance_index"),
                "ratio_pct": a.get("ratio_pct"),
                "boundary": a.get("boundary"),
                "deviation": a.get("deviation"),
                "cause": a.get("cause"),
                "actual_kwh": a.get("actual_kwh"),
                "expected_matched_kwh": a.get("expected_matched_kwh"),
            }
            for a in (snapshot.get("arrays") or [])
        ],
        "daily_row_count": len(daily_rows),
    }

    return {
        "assumptions": assumptions,
        "daily_rows": daily_rows,
        "summary": summary,
    }


_CSV_HEADER = (
    "array_id,array_name,day,measured_kwh,expected_kwh,residual,pi_day,boundary"
)


def auditor_csv_bytes(daily_rows: list[dict]) -> bytes:
    """UTF-8 CSV with fixed header. Empty values as blank cells — never NaN."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "array_id", "array_name", "day", "measured_kwh",
        "expected_kwh", "residual", "pi_day", "boundary",
    ])
    for row in daily_rows or []:
        writer.writerow([
            "" if row.get("array_id") is None else row.get("array_id"),
            "" if row.get("array_name") is None else row.get("array_name"),
            "" if row.get("day") is None else row.get("day"),
            "" if row.get("measured_kwh") is None else row.get("measured_kwh"),
            "" if row.get("expected_kwh") is None else row.get("expected_kwh"),
            "" if row.get("residual") is None else row.get("residual"),
            "" if row.get("pi_day") is None else row.get("pi_day"),
            "" if row.get("boundary") is None else row.get("boundary"),
        ])
    # utf-8 without BOM; tests assert header present
    return buf.getvalue().encode("utf-8")


def auditor_zip_bytes(snapshot: dict) -> bytes:
    """Zip with assumptions.json, summary.json, daily.csv."""
    payload = build_auditor_export(snapshot)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "assumptions.json",
            json.dumps(payload["assumptions"], indent=2, default=str) + "\n",
        )
        zf.writestr(
            "summary.json",
            json.dumps(payload["summary"], indent=2, default=str) + "\n",
        )
        zf.writestr("daily.csv", auditor_csv_bytes(payload["daily_rows"]))
    return out.getvalue()
