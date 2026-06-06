"""
Integration test: pro-rate algorithm vs Chester (array 53984) master workbook.

23 hard-coded bills from Bruce's billing history. 18 months of master MWh
values from his hand-built GMCS.xlsx source-of-truth.

Asserts that pro-rate MAE < 1.5 MWh/month (period_start MAE is ~1.7+).
Run with pytest -s to see per-month delta table.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from api.bill_attribution import distribute_kwh_by_calendar_day

# Chester bills: (period_start_str, period_end_str, kwh_generated)
CHESTER_BILLS = [
    ("2024-04-13", "2024-05-14", 25400),
    ("2024-05-15", "2024-06-12", 26400),
    ("2024-06-13", "2024-07-11", 28800),
    ("2024-07-12", "2024-08-13", 28280),
    ("2024-08-14", "2024-09-12", 26400),
    ("2024-09-13", "2024-10-14", 25280),
    ("2024-10-15", "2024-11-13", 21520),
    ("2024-11-14", "2024-12-11", 14200),
    ("2024-12-12", "2025-01-10", 12040),
    ("2025-01-11", "2025-02-11", 16960),
    ("2025-02-12", "2025-03-12", 20840),
    ("2025-03-13", "2025-04-10", 20000),
    ("2025-04-11", "2025-05-12", 24960),
    ("2025-05-13", "2025-06-11", 23280),
    ("2025-06-12", "2025-07-11", 27040),
    ("2025-07-12", "2025-08-11", 30200),
    ("2025-08-12", "2025-09-10", 28000),
    ("2025-09-11", "2025-10-10", 26360),
    ("2025-10-11", "2025-11-10", 18360),
    ("2025-11-11", "2025-12-10", 10680),
    ("2025-12-11", "2026-01-09", 11040),
    ("2026-01-10", "2026-02-10", 18360),
    ("2026-02-11", "2026-03-12", 16360),
]

# Master MWh values from Bruce's GMCS.xlsx source-of-truth
MASTER_MWH: dict[tuple[int, int], float] = {
    (2024, 7): 28.468, (2024, 8): 24.852, (2024, 9): 25.720,
    (2024, 10): 24.825, (2024, 11): 17.275, (2024, 12): 12.673,
    (2025, 1): 15.672, (2025, 2): 16.116, (2025, 3): 22.690,
    (2025, 4): 24.632, (2025, 5): 20.973, (2025, 6): 26.745,
    (2025, 7): 29.210, (2025, 8): 29.730, (2025, 9): 25.723,
    (2025, 10): 22.675, (2025, 11): 12.528, (2025, 12): 11.620,
}


def _parse_date(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def _run_prorate() -> dict[tuple[int, int], float]:
    """Return pro-rated kWh (not MWh) by calendar month across all Chester bills."""
    totals: dict[tuple[int, int], float] = {}
    for ps, pe, kwh in CHESTER_BILLS:
        b = SimpleNamespace(
            period_start=_parse_date(ps),
            period_end=_parse_date(pe),
            bill_date=None,
            kwh_generated=kwh,
        )
        for ym, kw in distribute_kwh_by_calendar_day(b).items():
            totals[ym] = totals.get(ym, 0.0) + kw
    return totals


def _run_period_start() -> dict[tuple[int, int], float]:
    """Return period_start-attributed kWh by calendar month (old behavior)."""
    totals: dict[tuple[int, int], float] = {}
    for ps, pe, kwh in CHESTER_BILLS:
        ps_date = _parse_date(ps)
        ym = (ps_date.year, ps_date.month)
        totals[ym] = totals.get(ym, 0.0) + kwh
    return totals


def test_chester_prorate_mae_beats_period_start(capsys):
    prorate = _run_prorate()
    period_start = _run_period_start()

    prorate_errors = []
    ps_errors = []
    prorate_closer_count = 0

    print("\n" + "=" * 72)
    print(f"{'Month':<10}  {'Master':>8}  {'Prorate':>8}  {'PeriodStart':>11}  "
          f"{'PR Err':>7}  {'PS Err':>7}  {'Winner':>8}")
    print("-" * 72)

    for ym in sorted(MASTER_MWH.keys()):
        master = MASTER_MWH[ym]
        pr_mwh = prorate.get(ym, 0.0) / 1000.0
        ps_mwh = period_start.get(ym, 0.0) / 1000.0
        pr_err = abs(pr_mwh - master)
        ps_err = abs(ps_mwh - master)
        prorate_errors.append(pr_err)
        ps_errors.append(ps_err)
        if pr_err < ps_err:
            prorate_closer_count += 1
            winner = "prorate"
        else:
            winner = "period_st"
        print(f"{ym[0]}-{ym[1]:02d}     {master:8.3f}  {pr_mwh:8.3f}  {ps_mwh:11.3f}  "
              f"{pr_err:7.3f}  {ps_err:7.3f}  {winner:>8}")

    pr_mae = sum(prorate_errors) / len(prorate_errors)
    ps_mae = sum(ps_errors) / len(ps_errors)
    print("-" * 72)
    print(f"MAE      {' ':8}  {' ':8}  {' ':11}  {pr_mae:7.3f}  {ps_mae:7.3f}")
    print(f"Prorate closer in {prorate_closer_count}/{len(MASTER_MWH)} months")
    print("=" * 72)

    assert pr_mae < 1.5, (
        f"Pro-rate MAE {pr_mae:.3f} MWh/month exceeds 1.5 threshold. "
        f"Period-start MAE was {ps_mae:.3f}."
    )
    # pro-rate should beat period_start overall
    assert pr_mae < ps_mae, (
        f"Pro-rate MAE {pr_mae:.3f} is not better than period_start MAE {ps_mae:.3f}"
    )
