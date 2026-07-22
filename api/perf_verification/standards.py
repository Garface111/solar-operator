"""IEC 61724-aligned method language for reports, API, and marketing.

We are not third-party certified. Language is careful: "aligned with" / "consistent
with" IEC 61724-1 / 61724-3 practice for monitoring and performance evaluation —
never "certified to IEC …".
"""
from __future__ import annotations

IEC_ALIGNMENT_NOTE = (
    "EnergyAgent Performance Verification evaluates PV systems using methods "
    "consistent with IEC 61724-1 (monitoring) and IEC 61724-3 (energy evaluation). "
    "This is an operational verification layer, not a third-party bankability "
    "certification."
)

METHOD_SUMMARY = {
    "title": "Performance verification method",
    "standards": ["IEC 61724-1", "IEC 61724-3"],
    "alignment": IEC_ALIGNMENT_NOTE,
    "measurement_boundary": (
        "Preferred measured energy is utility / revenue-grade meter generation "
        "at the point of interconnection (POI) when available for the calendar day. "
        "When meter-day energy is absent, inverter / telemetry AC energy is used. "
        "Monthly bill prorations (bill_prorate, utility_meter smears) are never used "
        "as measured energy for Performance Index."
    ),
    "expected_energy": (
        "Expected AC energy is computed from plane-of-array (POA) irradiance "
        "(satellite/reanalysis via Open-Meteo global_tilted_irradiance), array "
        "nameplate kW, plane geometry (tilt/azimuth), and a labeled performance "
        "ratio (PR). Default PR = 0.84 unless the owner sets a site PR. "
        "expected_kwh = nameplate_kw × (POA_kWh/m² / 1.0) × PR."
    ),
    "kpis": {
        "performance_index": (
            "PI = measured_energy / expected_energy over matched days "
            "(days with both measured and expected). PI ≈ 1 means production "
            "aligned with the weather-and-PR model."
        ),
        "performance_ratio_assumed": (
            "The model PR is an input derate (inverter, wiring, temperature, "
            "soiling allowance, mismatch). It is always labeled assumed vs owner-set."
        ),
        "deviation": (
            "Daily residual r = (measured − expected) / expected. Portfolio and "
            "array summaries report mean residual and classify persistence."
        ),
        "availability": (
            "Where inverter status exists, availability distinguishes all-in energy "
            "from in-service periods (excluding multi-day comm_gap / dead windows). "
            "Null when status history is insufficient — never fabricated."
        ),
    },
    "windows": (
        "Consistent calendar windows: daily, rolling N-day (default 14/30), and "
        "prior full calendar month for scheduled reports. Partial current day is "
        "excluded from matched comparisons."
    ),
    "honesty": (
        "No fabricated irradiance, measured energy, or PI. Unavailable inputs yield "
        "structured nulls with reasons."
    ),
}

REPORT_FOOTER = (
    "Performance Verification · methods consistent with IEC 61724-1 / 61724-3 · "
    "EnergyAgent Array Operator · not a third-party certification · "
    "Measured energy uses the utility meter when available, otherwise inverter AC · "
    "Expected energy is weather irradiance (POA) × nameplate × performance ratio"
)

DEFAULT_DEVIATION_THRESHOLD = 0.05  # 5% persistent-deviation floor (Sunreport parity)
DEFAULT_REPORT_WINDOW_DAYS = 30
