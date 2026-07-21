"""Persistence-aware deviation classification + priority scoring.

Class labels (contract language):
  - sudden: sharp break vs recent baseline
  - persistent: multi-day underperformance below threshold
  - seasonal: similar underperformance in same calendar month last year (if data)
  - none / ok: no underperformance signal
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import Optional, Sequence

DEFAULT_DEVIATION_THRESHOLD = 0.05  # 5%
DEFAULT_PERSISTENT_DAYS = 5


@dataclass
class DeviationClassification:
    label: str  # sudden | persistent | seasonal | ok | insufficient_data
    threshold: float
    magnitude_mean: Optional[float]  # mean residual on underperforming days (≤0)
    duration_days: int
    priority: float  # 0..100
    detail: str


def _residuals(series: Sequence[dict]) -> list[tuple[date, float]]:
    """series items: {day: date|str, residual: float|None} residual=(m-e)/e."""
    out: list[tuple[date, float]] = []
    for row in series:
        r = row.get("residual")
        if r is None:
            continue
        d = row.get("day")
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        elif isinstance(d, datetime):
            d = d.date()
        if not isinstance(d, date):
            continue
        try:
            out.append((d, float(r)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def priority_score(
    magnitude_mean_abs: float,
    duration_days: int,
    *,
    sudden: bool = False,
) -> float:
    """magnitude × duration → 0..100. Sudden multiplies 1.5."""
    if magnitude_mean_abs <= 0 or duration_days <= 0:
        return 0.0
    raw = magnitude_mean_abs * 100.0 * (duration_days / 7.0)
    if sudden:
        raw *= 1.5
    return round(min(100.0, max(0.0, raw)), 1)


def classify_deviation(
    series: Sequence[dict],
    *,
    threshold: float = DEFAULT_DEVIATION_THRESHOLD,
    persistent_days: int = DEFAULT_PERSISTENT_DAYS,
    prior_year_residuals: Optional[Sequence[float]] = None,
) -> DeviationClassification:
    """Classify underperformance pattern from daily residuals."""
    thr = abs(float(threshold or DEFAULT_DEVIATION_THRESHOLD))
    pts = _residuals(series)
    if len(pts) < 2:
        return DeviationClassification(
            label="insufficient_data",
            threshold=thr,
            magnitude_mean=None,
            duration_days=0,
            priority=0.0,
            detail="Need at least 2 matched days with residual to classify deviation.",
        )

    residuals = [r for _, r in pts]
    under = [(d, r) for d, r in pts if r < -thr]

    # Longest trailing underperforming streak
    streak = 0
    for _, r in reversed(pts):
        if r < -thr:
            streak += 1
        else:
            break

    # Sudden: last 1–2 days bad, prior week median near zero
    sudden = False
    if len(pts) >= 5:
        last_n = residuals[-2:]
        prior = residuals[:-2]
        if prior and all(r < -thr for r in last_n):
            try:
                prior_med = median(prior[-7:])
            except Exception:
                prior_med = 0.0
            if prior_med > -thr * 0.5:
                sudden = True

    # Seasonal: prior year same-window mean residual also under threshold
    seasonal = False
    if prior_year_residuals:
        py = [float(x) for x in prior_year_residuals if x is not None]
        if len(py) >= 5 and median(py) < -thr:
            seasonal = True

    if sudden and streak <= 3:
        mag = sum(residuals[-2:]) / 2.0
        pr = priority_score(abs(mag), max(streak, 2), sudden=True)
        return DeviationClassification(
            label="sudden",
            threshold=thr,
            magnitude_mean=round(mag, 4),
            duration_days=max(streak, 1),
            priority=pr,
            detail=(
                f"Sudden drop: last days residual < −{thr:.0%} while prior "
                f"baseline was near expected."
            ),
        )

    if streak >= persistent_days or len(under) >= persistent_days:
        mags = [r for _, r in under] or residuals[-streak:]
        mag = sum(mags) / len(mags)
        label = "seasonal" if seasonal else "persistent"
        pr = priority_score(abs(mag), max(streak, len(under)), sudden=False)
        return DeviationClassification(
            label=label,
            threshold=thr,
            magnitude_mean=round(mag, 4),
            duration_days=max(streak, len(under)),
            priority=pr,
            detail=(
                f"{'Seasonal pattern: ' if seasonal else ''}"
                f"Underperformance below −{thr:.0%} for "
                f"{max(streak, len(under))} day(s)."
            ),
        )

    if under:
        mag = sum(r for _, r in under) / len(under)
        pr = priority_score(abs(mag), len(under), sudden=False)
        return DeviationClassification(
            label="persistent" if len(under) >= 3 else "ok",
            threshold=thr,
            magnitude_mean=round(mag, 4),
            duration_days=len(under),
            priority=pr if len(under) >= 3 else 0.0,
            detail=(
                f"{len(under)} day(s) below −{thr:.0%} residual; "
                f"below persistence threshold of {persistent_days} days."
                if len(under) < 3
                else f"Emerging underperformance ({len(under)} days)."
            ),
        )

    return DeviationClassification(
        label="ok",
        threshold=thr,
        magnitude_mean=round(sum(residuals) / len(residuals), 4),
        duration_days=0,
        priority=0.0,
        detail="No multi-day underperformance below the deviation threshold.",
    )
