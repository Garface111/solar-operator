"""Plausible cause taxonomy for performance deviations.

Labels are heuristic — always present with confidence low|medium and plain-English
detail. Never claim certainty.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

# Canonical taxonomy (Sunreport-parity + our ops labels)
CAUSES = (
    "electrical",
    "availability",
    "shading",
    "soiling",
    "environmental",
    "data_quality",
    "unknown",
)


@dataclass
class CauseInference:
    cause: str
    confidence: str  # low | medium
    detail: str
    dollars_hint: Optional[str] = None


def infer_cause(
    *,
    deviation_label: str,
    residual_mean: Optional[float],
    peer_status: Optional[str] = None,
    expected_low: bool = False,
    sunny_day_residuals: Optional[Sequence[float]] = None,
    cloudy_day_residuals: Optional[Sequence[float]] = None,
    measured_days: int = 0,
    window_days: int = 14,
    boundary: str = "inverter",
    fault_code: Optional[str] = None,
) -> CauseInference:
    """Rule stack — first strong signal wins."""
    # Data quality first when evidence is thin
    if measured_days < max(3, window_days // 4) or boundary == "unavailable":
        return CauseInference(
            cause="data_quality",
            confidence="medium",
            detail=(
                "Too few matched measured days (or no measured boundary) to "
                "attribute a physical cause. Fix capture / meter feed first."
            ),
        )

    if fault_code or peer_status in ("fault", "dead"):
        return CauseInference(
            cause="electrical",
            confidence="medium",
            detail=(
                f"Hardware/electrical signal: status={peer_status or 'fault'}"
                + (f", code={fault_code}" if fault_code else "")
                + ". Check inverter faults and AC/DC wiring before soiling claims."
            ),
        )

    if peer_status == "comm_gap":
        return CauseInference(
            cause="availability",
            confidence="medium",
            detail=(
                "Communications gap — production may be fine but telemetry is dark. "
                "Re-auth portal / Auto-refresh before dispatching field service."
            ),
        )

    if expected_low:
        return CauseInference(
            cause="shading",
            confidence="medium",
            detail=(
                "Site marked expected-low (known shading / fixed derate). "
                "Judged against its baseline, not full nameplate peers."
            ),
        )

    # Soiling heuristic: persistent underperformance worse on sunny days
    if deviation_label in ("persistent", "seasonal") and sunny_day_residuals:
        sunny = [float(x) for x in sunny_day_residuals if x is not None]
        cloudy = [float(x) for x in (cloudy_day_residuals or []) if x is not None]
        if sunny:
            s_mean = sum(sunny) / len(sunny)
            c_mean = (sum(cloudy) / len(cloudy)) if cloudy else 0.0
            if s_mean < -0.05 and (not cloudy or s_mean < c_mean - 0.03):
                return CauseInference(
                    cause="soiling",
                    confidence="low",
                    detail=(
                        "Persistent shortfall more pronounced on high-irradiance days — "
                        "compatible with soiling or module surface losses. Confirm with "
                        "site inspection / cleaning trial."
                    ),
                )

    if deviation_label == "sudden":
        return CauseInference(
            cause="electrical",
            confidence="low",
            detail=(
                "Sudden step-change vs weather-expected output — often a string/inverter "
                "event or curtailment. Check fault logs and recent work orders."
            ),
        )

    if deviation_label in ("persistent", "seasonal") and residual_mean is not None and residual_mean < -0.05:
        return CauseInference(
            cause="environmental" if deviation_label == "seasonal" else "unknown",
            confidence="low",
            detail=(
                "Sustained gap vs expected energy without a clear hardware flag. "
                "Could be soiling, vegetation, snow, mis-set PR/geometry, or model "
                "mismatch — treat as investigation priority, not a confirmed root cause."
            ),
        )

    return CauseInference(
        cause="unknown",
        confidence="low",
        detail="No strong cause signal; performance near model or mixed residuals.",
    )


def cause_to_dict(c: CauseInference) -> dict[str, Any]:
    return {
        "cause": c.cause,
        "confidence": c.confidence,
        "detail": c.detail,
        "dollars_hint": c.dollars_hint,
    }
