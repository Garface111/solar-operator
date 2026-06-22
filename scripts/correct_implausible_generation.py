#!/usr/bin/env python3
"""Correction sweep for the generation watchdog.

Replaces physically-impossible DailyGeneration / InverterDaily kWh rows (a
cumulative/billing-period value that leaked into a single day's slot) with the
MEDIAN of that array's / inverter's sane days. AO bills per-kWh, so an impossible
daily row over-invoices — this is the remediation the watchdog alert references.

SAFE:
  * DRY-RUN by default — prints every proposed change, writes NOTHING.
  * Set CORRECT_EXECUTE=1 to apply.
  * Reversible: the original value is preserved in the row's `source` field
    (corr_was_<old>). Skips rows with no sane history (flags for manual review —
    never fabricates a number).

Run (read-only):  python -m scripts.correct_implausible_generation
Apply:            CORRECT_EXECUTE=1 python -m scripts.correct_implausible_generation
"""
import os
from datetime import date as _date
from statistics import median

from sqlalchemy import select

from api.db import SessionLocal
from api.models import DailyGeneration, InverterDaily
from api.jobs.generation_watchdog import scan_implausible_generation

EXECUTE = os.getenv("CORRECT_EXECUTE") == "1"


def _median_sane(db, model, id_col, id_val, ceiling):
    vals = [r.kwh for r in db.execute(
        select(model).where(id_col == id_val, model.kwh > 0, model.kwh <= ceiling)
    ).scalars().all()]
    return median(vals) if vals else None


def main():
    scan = scan_implausible_generation()
    print(f"watchdog: {len(scan['daily'])} daily + {len(scan['inverter'])} inverter implausible rows")
    print(f"MODE: {'EXECUTE (writing)' if EXECUTE else 'DRY-RUN (no writes)'}\n")
    would = fixed = skipped = 0
    with SessionLocal() as db:
        for b in scan["daily"]:
            med = _median_sane(db, DailyGeneration, DailyGeneration.array_id, b["array_id"], b["ceiling"])
            tag = f"[DAILY]    {b['tenant']} · {b['array']} {b['day']}: {b['kwh']:,.0f} kWh (ceiling {b['ceiling']:,.0f})"
            if med is None:
                print(f"  SKIP {tag} — no sane history to median (manual review)"); skipped += 1; continue
            print(f"  FIX  {tag}  ->  {med:,.1f} kWh (median of sane days)"); would += 1
            if EXECUTE:
                row = db.execute(select(DailyGeneration).where(
                    DailyGeneration.array_id == b["array_id"],
                    DailyGeneration.day == _date.fromisoformat(b["day"]),
                )).scalars().first()
                if row:
                    row.source = f"corr_was_{int(row.kwh)}"[:32]
                    row.kwh = float(med)
                    db.commit(); fixed += 1
        for b in scan["inverter"]:
            med = _median_sane(db, InverterDaily, InverterDaily.inverter_id, b["inverter_id"], b["ceiling"])
            tag = f"[INVERTER] {b['tenant']} · {b['inverter']} {b['day']}: {b['kwh']:,.0f} kWh (ceiling {b['ceiling']:,.0f})"
            if med is None:
                print(f"  SKIP {tag} — no sane history (manual review)"); skipped += 1; continue
            print(f"  FIX  {tag}  ->  {med:,.1f} kWh"); would += 1
            if EXECUTE:
                row = db.execute(select(InverterDaily).where(
                    InverterDaily.inverter_id == b["inverter_id"],
                    InverterDaily.day == _date.fromisoformat(b["day"]),
                )).scalars().first()
                if row:
                    if hasattr(row, "source"):
                        row.source = f"corr_was_{int(row.kwh)}"[:32]
                    row.kwh = float(med)
                    db.commit(); fixed += 1
    if EXECUTE:
        print(f"\nAPPLIED {fixed} corrections, {skipped} skipped.")
    else:
        print(f"\nWOULD APPLY {would} corrections, {skipped} skipped. Re-run with CORRECT_EXECUTE=1 to write.")


if __name__ == "__main__":
    main()
