"""
READ-ONLY diagnostic: why our GMCS monthly generation numbers diverge from the
values Crown REC Services submits to NEPOOL-GIS.

Context
-------
Ford compared our automated GMCS workbook against the GMCS.xlsx that Crown sent
Bruce. Format is nearly identical, but the *monthly* MWh differ on every array.
Quarter totals are close (~1-4%) while the split across the three months is off
by up to ~5 MWh. Our workbook's within-quarter month spread is ~20% smaller than
Crown's — the fingerprint of calendar-day *bill proration* flattening the true
monthly peaks/troughs (see api/bill_attribution.distribute_kwh_by_calendar_day).

Hypothesis
----------
For an array with no DailyGeneration coverage, build_workbook falls back to
prorating each utility *bill* across calendar days — which redistributes a
billing-period total across months instead of using the true monthly meter
generation Crown reports. Arrays WITH daily coverage should match Crown much
better (daily sums ≈ real monthly generation).

What this script does (READ-ONLY — no writes, no commits)
---------------------------------------------------------
For each producing array under the tenant, it reproduces build_workbook's EXACT
per-month sourcing:
  * daily  = _daily_generation_by_month(array)             (preferred source)
  * bill   = sum of distribute_kwh_by_calendar_day(bill)   (fallback)
  * chosen = daily if that month has daily data else bill  (the {**bill,**daily} merge)
and reports, per array:
  * how many months in the window are daily-backed vs bill-only
  * for the 4 arrays we have Crown ground-truth for, the per-month delta between
    `chosen` and Crown, split by source — so we can see whether bill-only months
    diverge while daily-backed months match.

Run it IN the Railway container (the script is NOT deployed), READ-ONLY:
  railway ssh "cat > /app/diag_crown_parity.py && cd /app && \
      PYTHONPATH=/app python diag_crown_parity.py --tenant ten_14b76982523a3b47; \
      rm -f /app/diag_crown_parity.py" < scripts/diag_crown_parity.py

(Bruce's live tenant is ten_14b76982523a3b47. Pass --tenant to target another.)
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, Bill, DailyGeneration
from api.bill_attribution import distribute_kwh_by_calendar_day
from api.writers.gmcs_writer import (
    _rolling_quarters,
    _quarter_months,
    _daily_generation_by_month,
    default_reporting_reference_date,
)

# Crown REC ground-truth (monthly MWh) transcribed from the GMCS.xlsx Bruce
# received. Keyed by array name; only the 4 arrays present in that file.
CROWN_TRUTH = json.loads(r'''
{
  "Chester": {"nepool_gis_id": "53984", "monthly_mwh": {"2024-07": 28.468, "2024-08": 24.852, "2024-09": 25.72, "2024-10": 24.825, "2024-11": 17.275, "2024-12": 12.673, "2025-01": 15.672, "2025-02": 16.116, "2025-03": 22.69, "2025-04": 24.632, "2025-05": 20.973, "2025-06": 26.745, "2025-07": 29.21, "2025-08": 29.73, "2025-09": 25.723, "2025-10": 22.675, "2025-11": 12.528, "2025-12": 11.62, "2026-01": 12.608, "2026-02": 16.924, "2026-03": 21.061}},
  "Tannery Brook": {"nepool_gis_id": "46425", "monthly_mwh": {"2024-07": 21.951, "2024-08": 16.956, "2024-09": 17.732, "2024-10": 15.331, "2024-11": 7.841, "2024-12": 1.935, "2025-01": 3.854, "2025-02": 1.297, "2025-03": 14.779, "2025-04": 17.552, "2025-05": 15.831, "2025-06": 19.067, "2025-07": 17.981, "2025-08": 17.307, "2025-09": 17.952, "2025-10": 13.648, "2025-11": 6.109, "2025-12": 0.894, "2026-01": 1.341, "2026-02": 1.918, "2026-03": 13.371}},
  "Timberworks": {"nepool_gis_id": "61959", "monthly_mwh": {"2024-07": 28.35, "2024-08": 22.932, "2024-09": 22.059, "2024-10": 23.387, "2024-11": 11.526, "2024-12": 4.298, "2025-01": 6.985, "2025-02": 4.134, "2025-03": 20.656, "2025-04": 21.958, "2025-05": 20.488, "2025-06": 25.805, "2025-07": 27.499, "2025-08": 26.947, "2025-09": 27.564, "2025-10": 18.919, "2025-11": 8.82, "2025-12": 2.454, "2026-01": 4.043, "2026-02": 7.545, "2026-03": 15.518}},
  "Waterford": {"nepool_gis_id": "78671", "monthly_mwh": {"2024-07": 28.752, "2024-08": 23.255, "2024-09": 25.599, "2024-10": 20.856, "2024-11": 9.955, "2024-12": 4.027, "2025-01": 11.349, "2025-02": 9.894, "2025-03": 20.811, "2025-04": 21.855, "2025-05": 20.862, "2025-06": 25.699, "2025-07": 27.663, "2025-08": 28.218, "2025-09": 26.351, "2025-10": 18.167, "2025-11": 7.721, "2025-12": 3.37, "2026-01": 4.539, "2026-02": 8.801, "2026-03": 18.423}}
}
''')


def _window(quarters: int = 6, reference_date: date | None = None):
    ref = reference_date or default_reporting_reference_date(date.today())
    qlist = _rolling_quarters(ref, count=quarters)
    months = [m for (qy, qq) in qlist for m in _quarter_months(qy, qq)]
    start_y, start_q = qlist[0]
    report_start = date(start_y, (start_q - 1) * 3 + 1, 1)
    end_y, end_q = qlist[-1]
    end_m = end_q * 3
    report_end = (date(end_y, 12, 31) if end_m == 12
                  else date(end_y, end_m + 1, 1) - timedelta(days=1))
    return qlist, months, report_start, report_end


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True, help="tenant id, e.g. ten_14b76982523a3b47")
    ap.add_argument("--quarters", type=int, default=6)
    args = ap.parse_args()

    qlist, months, report_start, report_end = _window(args.quarters)
    print(f"tenant={args.tenant}  window={qlist[0]}..{qlist[-1]}  "
          f"({report_start}..{report_end})  quarters={args.quarters}")
    print("(window uses the production default: NEPOOL minting quarter, so it "
          "ends on the same quarter Crown submits)\n")

    grand = {"daily_months": 0, "bill_months": 0, "none_months": 0}
    parity_rows = []  # (array, month, source, ours, crown, delta)

    with SessionLocal() as db:
        clients = db.execute(
            select(Client).where(Client.tenant_id == args.tenant,
                                 Client.deleted_at.is_(None))
        ).scalars().all()
        client_ids = [c.id for c in clients]
        arrays = db.execute(
            select(Array).where(Array.client_id.in_(client_ids),
                                Array.excluded.is_(False),
                                Array.deleted_at.is_(None))
        ).scalars().all() if client_ids else []

        print(f"{len(clients)} client(s), {len(arrays)} non-excluded array(s)\n")
        print(f"{'ARRAY':28} {'gis_id':7} {'daily_mo':>8} {'bill_mo':>7} "
              f"{'none_mo':>7}  vs Crown (mean |Δ| MWh, by source)")
        print("-" * 96)

        for arr in sorted(arrays, key=lambda a: a.name or ""):
            accts = db.execute(
                select(UtilityAccount).where(UtilityAccount.array_id == arr.id)
            ).scalars().all()
            # bill-prorated kWh per month for this array
            bill_by_month: dict[tuple[int, int], float] = defaultdict(float)
            if accts:
                bills = db.execute(
                    select(Bill).where(Bill.account_id.in_([a.id for a in accts]))
                ).scalars().all()
                for b in bills:
                    for (yy, mm), kwh in distribute_kwh_by_calendar_day(b).items():
                        bill_by_month[(yy, mm)] += kwh
            # daily generation per month (preferred source)
            daily = _daily_generation_by_month(db, arr.id, report_start, report_end)
            # per-month daily row count = coverage signal
            daily_rows = db.execute(
                select(DailyGeneration.day).where(
                    DailyGeneration.array_id == arr.id,
                    DailyGeneration.day >= report_start,
                    DailyGeneration.day <= report_end)
            ).all()
            rows_by_month: dict[tuple[int, int], int] = defaultdict(int)
            for (d,) in daily_rows:
                rows_by_month[(d.year, d.month)] += 1

            n_daily = n_bill = n_none = 0
            truth = CROWN_TRUTH.get(arr.name, {}).get("monthly_mwh", {})
            deltas = {"daily": [], "bill": []}
            for (yy, mm) in months:
                has_daily = (yy, mm) in daily
                if has_daily:
                    source = "daily"; kwh = daily[(yy, mm)]; n_daily += 1
                elif (yy, mm) in bill_by_month:
                    source = "bill"; kwh = bill_by_month[(yy, mm)]; n_bill += 1
                else:
                    source = "none"; kwh = 0.0; n_none += 1
                ours_mwh = round(kwh / 1000.0, 3)
                key = f"{yy}-{mm:02d}"
                if key in truth:
                    d = round(ours_mwh - truth[key], 3)
                    deltas[source if source != "none" else "bill"].append(abs(d))
                    parity_rows.append((arr.name, key, source, ours_mwh, truth[key], d))

            grand["daily_months"] += n_daily
            grand["bill_months"] += n_bill
            grand["none_months"] += n_none
            md = (f"{sum(deltas['daily'])/len(deltas['daily']):.2f}"
                  if deltas["daily"] else "  -")
            mb = (f"{sum(deltas['bill'])/len(deltas['bill']):.2f}"
                  if deltas["bill"] else "  -")
            crown_note = f"daily={md} bill={mb}" if arr.name in CROWN_TRUTH else ""
            print(f"{(arr.name or '')[:28]:28} {(arr.nepool_gis_id or '—'):7} "
                  f"{n_daily:>8} {n_bill:>7} {n_none:>7}  {crown_note}")

    print("\n" + "=" * 96)
    print(f"TOTALS across all arrays: daily-backed months={grand['daily_months']}, "
          f"bill-only months={grand['bill_months']}, no-data months={grand['none_months']}")

    if parity_rows:
        print("\nPer-month parity vs Crown (the 4 known arrays):")
        print(f"  {'array':16} {'month':8} {'src':6} {'ours':>8} {'crown':>8} {'Δ':>8}")
        for a, k, s, o, c, d in parity_rows:
            flag = "  <<" if abs(d) >= 1.0 else ""
            print(f"  {a[:16]:16} {k:8} {s:6} {o:8.3f} {c:8.3f} {d:+8.3f}{flag}")
        db_deltas = [abs(r[5]) for r in parity_rows if r[2] == "bill"]
        dl_deltas = [abs(r[5]) for r in parity_rows if r[2] == "daily"]
        print("\nINTERPRETATION")
        if db_deltas:
            print(f"  bill-sourced months: mean |Δ| vs Crown = "
                  f"{sum(db_deltas)/len(db_deltas):.2f} MWh  (n={len(db_deltas)})")
        if dl_deltas:
            print(f"  daily-sourced months: mean |Δ| vs Crown = "
                  f"{sum(dl_deltas)/len(dl_deltas):.2f} MWh  (n={len(dl_deltas)})")
        print("  → If bill months diverge and daily months match, the fix is to feed "
              "true monthly/daily generation, not prorated bills.")
        print("  → If BOTH diverge, our underlying kWh source differs from Crown's "
              "meter reads — escalate the data-source question to Bruce.")


if __name__ == "__main__":
    main()
