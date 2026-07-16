"""Raw utility generation export (Ford 2026-07-16).

A NEPOOL Operator can download a client's utility generation for a chosen
quarter, organized by month — the meter data behind the REC report, shaped for a
monthly-basis program to ingest. Covers ALL of the client's utilities:
  • GMP — the 15-minute interval meter (api.reports.gmp_daily_read) + the bill's
    reported generation where no interval exists.
  • SmartHub co-ops (VEC/WEC/…) — the daily RETURN-meter generation captured into
    DailyGeneration (co-op bills carry no kwh_generated, so their generation is in
    the meter feed, not the bill).
Inverter/vendor telemetry (solaredge/fronius/…) is never included — this is
utility-measured generation, the REC basis.

Workbook:
  • "Monthly Summary" — projects × the quarter's 3 months grid + totals, with the
    utility + source per project, so every project shows.
  • a per-project daily detail sheet wherever a daily meter feed exists.
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
from ..generation_sources import EXTENSION_SOURCES, VENDOR_TELEMETRY_SOURCES
from ..models import Array, Bill, Client, DailyGeneration, UtilityAccount
from ..reports import gmp_daily_read
from .gmcs_writer import _daily_generation_by_month, _quarter_months, _sheet_name_for_array

# Non-utility DailyGeneration sources — never in a utility-generation export.
_METER_EXCLUDE = VENDOR_TELEMETRY_SOURCES | EXTENSION_SOURCES | {"bill_prorate"}

_GREEN = "064E3B"
_TITLE = Font(bold=True, size=14, color="1F4E2A")
_SUB = Font(italic=True, size=9, color="666666")
_HDR = Font(bold=True, size=11, color="FFFFFF")
_HDR_FILL = PatternFill("solid", fgColor=_GREEN)
_BOLD = Font(bold=True)
_MONTH = Font(bold=True, size=12, color="1F4E2A")
_CENTER = Alignment(horizontal="center")


def _accounts(db, array_id: int):
    return db.execute(
        select(UtilityAccount).where(UtilityAccount.array_id == array_id,
                                     UtilityAccount.deleted_at.is_(None))
    ).scalars().all()


def _providers_for_array(db, array_id: int) -> list[str]:
    return sorted({(ua.provider or "").upper() for ua in _accounts(db, array_id) if ua.provider})


def _bill_months(db, array_id: int) -> dict[tuple[int, int], float]:
    """Monthly generation from the utility BILLS (all of the array's utilities).
    Only GMP bills actually carry kwh_generated; co-op bills don't (their
    generation is in the meter feed), so they contribute 0 here — by design."""
    acct_ids = [ua.id for ua in _accounts(db, array_id)]
    out: dict[tuple[int, int], float] = defaultdict(float)
    if acct_ids:
        for b in db.execute(select(Bill).where(Bill.account_id.in_(acct_ids))).scalars().all():
            for key, kwh in distribute_kwh_by_calendar_day(b).items():
                out[key] += kwh
    return out


def _array_monthly(db, array_id: int, months, start, end):
    """Per (year,month) -> (kwh, source). The utility METER feed (GMP interval +
    SmartHub daily, vendor-excluded) wins where present; the GMP bill fills GMP
    months with no interval; 0 otherwise."""
    meter = _daily_generation_by_month(db, array_id, start, end)  # GMP interval + co-op daily
    bills = _bill_months(db, array_id)
    out = {}
    for key in months:
        if key in meter and meter[key]:
            out[key] = (meter[key], "meter")
        elif key in bills:
            out[key] = (bills[key], "bill")
        else:
            out[key] = (0.0, "")
    return out


def _daily_utility_series(db, array_id: int, start, end):
    """Daily utility-measured generation for an array — the co-op/utility daily
    meter (DailyGeneration, vendor-excluded), with the higher-fidelity GMP
    interval meter overlaid where present. [{day, kwh, intervals}] ascending."""
    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh)
        .where(DailyGeneration.array_id == array_id,
               DailyGeneration.day >= start, DailyGeneration.day <= end,
               func.lower(DailyGeneration.source).notin_(_METER_EXCLUDE))
    ).all()
    by_day = {d: {"kwh": round(float(k or 0.0), 3), "intervals": None} for d, k in rows}
    for r in gmp_daily_read.get_daily_series(array_id, start=start, end=end, db=db):
        by_day[r["day"]] = {"kwh": round(float(r["kwh"]), 3), "intervals": r["intervals"]}
    return [{"day": d, **v} for d, v in sorted(by_day.items())]


def _write_summary(sh, client_name, rows, months, year, quarter):
    ncols = len(months) + 3
    end_col = chr(ord("A") + ncols - 1)
    sh["A1"] = f"{client_name} — utility generation, Q{quarter} {year}"
    sh["A1"].font = _TITLE
    sh.merge_cells(f"A1:{end_col}1")
    sh["A2"] = "Generation (kWh) by month · utility meter (GMP interval / co-op daily) or GMP bill"
    sh["A2"].font = _SUB
    sh.merge_cells(f"A2:{end_col}2")

    hdr = ["Project"] + [f"{calendar.month_name[m]} {y}" for (y, m) in months] + ["Quarter total", "Utility · source"]
    for col, label in enumerate(hdr, start=1):
        c = sh.cell(4, col, label)
        c.font, c.fill, c.alignment = _HDR, _HDR_FILL, _CENTER
    r = 5
    col_tot = defaultdict(float)
    for name, monthly, providers in rows:
        sh.cell(r, 1, name)
        qtot = 0.0
        srcs = set()
        for i, key in enumerate(months):
            kwh, src = monthly[key]
            sh.cell(r, 2 + i, round(kwh, 1))
            qtot += kwh
            col_tot[i] += kwh
            if src:
                srcs.add(src)
        sh.cell(r, 2 + len(months), round(qtot, 1)).font = _BOLD
        col_tot["q"] += qtot
        util = "/".join(providers) if providers else "—"
        sh.cell(r, 3 + len(months), f"{util} · {' + '.join(sorted(srcs)) or 'no data'}")
        r += 1
    sh.cell(r, 1, "All projects").font = _BOLD
    for i in range(len(months)):
        sh.cell(r, 2 + i, round(col_tot[i], 1)).font = _BOLD
    sh.cell(r, 2 + len(months), round(col_tot["q"], 1)).font = _BOLD
    sh.column_dimensions["A"].width = 26
    for i in range(len(months) + 2):
        sh.column_dimensions[chr(ord("B") + i)].width = 17


def _write_daily(sh, arr, series, year, quarter):
    by_day = {r["day"]: r for r in series}
    nid = getattr(arr, "nepool_gis_id", None)
    sh["A1"] = f"{arr.name}" + (f"  ·  NEPOOL-GIS {nid}" if nid else "")
    sh["A1"].font = _TITLE
    sh.merge_cells("A1:C1")
    sh["A2"] = f"Daily utility meter · Q{quarter} {year}"
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
                sh.cell(row, 2, by_day[d]["kwh"])
                if by_day[d].get("intervals") is not None:
                    sh.cell(row, 3, by_day[d]["intervals"])
                mt += by_day[d]["kwh"]
            row += 1
        sh.cell(row, 1, f"{calendar.month_name[m]} total").font = _BOLD
        sh.cell(row, 2, round(mt, 3)).font = _BOLD
        row += 2
    sh.column_dimensions["A"].width = 14
    sh.column_dimensions["B"].width = 18
    sh.column_dimensions["C"].width = 11


def build_generation_workbook(client_id: int, out_path, *, year: int, quarter: int) -> Path:
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

        summary_rows = []
        detail = []  # (array, series)
        for arr in arrays:
            monthly = _array_monthly(db, arr.id, months, start, end)
            if any(v[0] for v in monthly.values()):
                summary_rows.append((arr.name, monthly, _providers_for_array(db, arr.id)))
            series = _daily_utility_series(db, arr.id, start, end)
            if series:
                detail.append((arr, series))

        wb = Workbook()
        summary = wb.active
        summary.title = "Monthly Summary"
        if summary_rows:
            _write_summary(summary, client.name, summary_rows, months, year, quarter)
        else:
            summary["A1"] = f"No utility generation for {client.name} in Q{quarter} {year}."
            summary["A1"].font = _TITLE

        used = {"Monthly Summary"}
        for arr, series in detail:
            sh = wb.create_sheet(title=_sheet_name_for_array(arr.name, used))
            _write_daily(sh, arr, series, year, quarter)

    wb.save(str(out_path))
    return out_path
