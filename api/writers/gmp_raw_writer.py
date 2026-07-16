"""Raw GMP generation export (Ford 2026-07-16).

A NEPOOL Operator can download a client's GMP generation for a chosen quarter,
organized by month — the underlying utility meter data behind the GMCS REC
report, shaped for a monthly-basis program to ingest. GMP data ONLY (never
inverter telemetry): the 15-minute interval meter (api.reports.gmp_daily_read)
where we have it, falling back to the GMP bill's reported generation where we
don't (some projects only have bills — e.g. London_SE).

Workbook shape:
  • "Monthly Summary" sheet — projects × the quarter's 3 months grid + totals,
    with the source per project (interval meter vs bill), so every project shows.
  • one detail sheet per project THAT HAS interval data — the raw daily kWh,
    organized by month, for the granular feed.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from sqlalchemy import func, select

from ..bill_attribution import distribute_kwh_by_calendar_day
from ..db import SessionLocal
from ..models import Array, Bill, Client, UtilityAccount
from ..reports import gmp_daily_read
from .gmcs_writer import _quarter_months, _sheet_name_for_array

_GREEN = "064E3B"
_TITLE = Font(bold=True, size=14, color="1F4E2A")
_SUB = Font(italic=True, size=9, color="666666")
_HDR = Font(bold=True, size=11, color="FFFFFF")
_HDR_FILL = PatternFill("solid", fgColor=_GREEN)
_BOLD = Font(bold=True)
_MONTH = Font(bold=True, size=12, color="1F4E2A")
_THIN = Border(*[Side(style="thin", color="E8E2D9")] * 4)
_CENTER = Alignment(horizontal="center")


def _bill_months(db, array_id: int) -> dict[tuple[int, int], float]:
    # GMP accounts only — this is the GMP generation export, so a co-op (VEC/…)
    # account on the same client must not leak in via its bills.
    acct_ids = [ua.id for ua in db.execute(
        select(UtilityAccount).where(UtilityAccount.array_id == array_id,
                                     UtilityAccount.deleted_at.is_(None),
                                     func.lower(UtilityAccount.provider) == "gmp")
    ).scalars().all()]
    out: dict[tuple[int, int], float] = defaultdict(float)
    if acct_ids:
        for b in db.execute(select(Bill).where(Bill.account_id.in_(acct_ids))).scalars().all():
            for (y, m), kwh in distribute_kwh_by_calendar_day(b).items():
                out[(y, m)] += kwh
    return out


def _array_monthly(db, array_id: int, months, start, end):
    """Per (year,month) -> (kwh, source). Interval meter wins where present; the
    GMP bill fills months with no interval; 0 with source '' when neither has it."""
    interval = {(r["year"], r["month"]): r["kwh"]
                for r in gmp_daily_read.get_monthly_totals(array_id, start=start, end=end, db=db)}
    bills = _bill_months(db, array_id)
    out = {}
    for key in months:
        if key in interval:
            out[key] = (interval[key], "interval meter")
        elif key in bills:
            out[key] = (bills[key], "bill")
        else:
            out[key] = (0.0, "")
    return out


def _write_summary(sh, client_name, arrays_monthly, months, year, quarter):
    sh["A1"] = f"{client_name} — GMP generation, Q{quarter} {year}"
    sh["A1"].font = _TITLE
    sh.merge_cells(f"A1:{chr(ord('A') + len(months) + 2)}1")
    sh["A2"] = "Generation (kWh) by month · GMP interval meter, or the GMP bill where no interval exists"
    sh["A2"].font = _SUB
    sh.merge_cells(f"A2:{chr(ord('A') + len(months) + 2)}2")

    hdr = ["Project"] + [f"{calendar.month_name[m]} {y}" for (y, m) in months] + ["Quarter total", "Source"]
    for col, label in enumerate(hdr, start=1):
        c = sh.cell(4, col, label)
        c.font, c.fill, c.alignment, c.border = _HDR, _HDR_FILL, _CENTER, _THIN
    row = 5
    col_totals = defaultdict(float)
    for name, monthly in arrays_monthly:
        sh.cell(row, 1, name)
        qtot = 0.0
        srcs = set()
        for i, key in enumerate(months):
            kwh, src = monthly[key]
            sh.cell(row, 2 + i, round(kwh, 1))
            qtot += kwh
            col_totals[i] += kwh
            if src:
                srcs.add(src)
        sh.cell(row, 2 + len(months), round(qtot, 1)).font = _BOLD
        col_totals["q"] += qtot
        sh.cell(row, 3 + len(months), " + ".join(sorted(srcs)) or "no data")
        row += 1
    # totals row
    sh.cell(row, 1, "All projects").font = _BOLD
    for i in range(len(months)):
        sh.cell(row, 2 + i, round(col_totals[i], 1)).font = _BOLD
    sh.cell(row, 2 + len(months), round(col_totals["q"], 1)).font = _BOLD
    sh.column_dimensions["A"].width = 26
    for i in range(len(months) + 2):
        sh.column_dimensions[chr(ord("B") + i)].width = 16


def _write_daily_detail(sh, arr, year, quarter, start, end):
    series = gmp_daily_read.get_daily_series(arr.id, start=start, end=end)
    by_day = {r["day"]: (r["kwh"], r["intervals"]) for r in series}
    nid = getattr(arr, "nepool_gis_id", None)
    sh["A1"] = f"{arr.name}" + (f"  ·  NEPOOL-GIS {nid}" if nid else "")
    sh["A1"].font = _TITLE
    sh.merge_cells("A1:C1")
    sh["A2"] = f"Raw GMP interval meter · daily kWh · Q{quarter} {year}"
    sh["A2"].font = _SUB
    sh.merge_cells("A2:C2")
    row = 4
    for (y, m) in _quarter_months(year, quarter):
        sh.cell(row, 1, f"{calendar.month_name[m]} {y}").font = _MONTH
        row += 1
        for col, label in enumerate(["Date", "Generation (kWh)", "Intervals"], start=1):
            c = sh.cell(row, col, label)
            c.font, c.fill, c.alignment = _HDR, _HDR_FILL, _CENTER
        row += 1
        mt = 0.0
        for dd in range(1, calendar.monthrange(y, m)[1] + 1):
            d = date(y, m, dd)
            sh.cell(row, 1, d.isoformat())
            if d in by_day:
                kwh, iv = by_day[d]
                sh.cell(row, 2, round(float(kwh), 3))
                sh.cell(row, 3, iv)
                mt += float(kwh)
            row += 1
        sh.cell(row, 1, f"{calendar.month_name[m]} total").font = _BOLD
        sh.cell(row, 2, round(mt, 3)).font = _BOLD
        row += 2
    sh.column_dimensions["A"].width = 14
    sh.column_dimensions["B"].width = 18
    sh.column_dimensions["C"].width = 11


def build_gmp_generation_workbook(client_id: int, out_path, *, year: int, quarter: int) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    months = _quarter_months(year, quarter)
    start = date(months[0][0], months[0][1], 1)
    ly, lm = months[-1]
    end = date(ly, lm, calendar.monthrange(ly, lm)[1])

    with SessionLocal() as db:
        client = db.get(Client, client_id)
        if client is None:
            raise ValueError("client not found")
        arrays = db.execute(
            select(Array).where(Array.client_id == client_id,
                                Array.deleted_at.is_(None),
                                Array.excluded.is_(False)).order_by(Array.name)
        ).scalars().all()

        arrays_monthly = []
        interval_arrays = []
        for arr in arrays:
            monthly = _array_monthly(db, arr.id, months, start, end)
            if any(v[0] for v in monthly.values()):
                arrays_monthly.append((arr.name, monthly))
            if gmp_daily_read.get_daily_series(arr.id, start=start, end=end, db=db):
                interval_arrays.append(arr)

        wb = Workbook()
        summary = wb.active
        summary.title = "Monthly Summary"
        if arrays_monthly:
            _write_summary(summary, client.name, arrays_monthly, months, year, quarter)
        else:
            summary["A1"] = f"No GMP generation for {client.name} in Q{quarter} {year}."
            summary["A1"].font = _TITLE

        used = {"Monthly Summary"}
        for arr in interval_arrays:
            sh = wb.create_sheet(title=_sheet_name_for_array(arr.name, used))
            _write_daily_detail(sh, arr, year, quarter, start, end)

    wb.save(str(out_path))
    return out_path
