"""
GMCS-format writer — mimics the Green Mountain Community Solar quarterly
NEPOOL-GIS-ready workbook used by Bruce.

Layout (one sheet per array):
  A1:C1 (merged) — "<Array Name> (<optional ID>)"
  Row 5 — header: Quarter | Generation (MWh) | Reporting Amount | RECs†
  Rows 7-29 — 6 quarter blocks × 3 month rows each
    Each quarter:
      - first row holds quarter label (e.g. "Q3 2024") in col A
      - cols B,C = generation in MWh (kWh / 1000, 3 decimals)
      - col D = whole RECs (floor of MWh)
      - one blank row between quarters
  Row 31 — footnote: "† NEPOOL-GIS will award 1 REC per whole MWh of generation."

Default window: most recent 6 complete quarters ending at the prior quarter
relative to the report-generation date (no in-progress quarters).
"""
from __future__ import annotations
import pathlib
from collections import defaultdict
from datetime import datetime, date
from typing import Optional

from sqlalchemy import select
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..db import SessionLocal, DATA_DIR
from ..models import Tenant, Client, UtilityAccount, Array, Bill


REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True, parents=True)


# Footnote text — VERBATIM from Bruce's GMCS.xlsx. Single source of truth so
# the demo writer (api/writers/demo_writer.py) can reuse the exact wording.
# Never paraphrase — see CLAUDE.md "GMCS writer format rules".
FOOTNOTE_TEXT = (
    " † NEPOOL-GIS will award 1 REC for every MWH reported.  "
    "Additionally, NEPOOL-GIS will keep track of the decimal "
    "MWHs and award an additional REC when the total exceeds 1 MWH."
)


# ── helpers ──────────────────────────────────────────────────────────
def _bill_target_month(bill: Bill) -> Optional[tuple[int, int]]:
    src = bill.period_start or bill.bill_date
    if not src:
        return None
    return src.year, src.month


def _quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def _month_in_quarter(month: int) -> int:
    """Returns 1, 2, or 3 — position of month within its quarter."""
    return ((month - 1) % 3) + 1


def _quarter_months(year: int, q: int) -> list[tuple[int, int]]:
    start = (q - 1) * 3 + 1
    return [(year, start + i) for i in range(3)]


def _rolling_quarters(ref: date, count: int = 6) -> list[tuple[int, int]]:
    """Return list of (year, quarter) for the most recent `count` complete
    quarters relative to `ref`. Most-recent quarter LAST in the list
    (chronological order for spreadsheet display)."""
    # Determine current quarter then step back one to skip the in-progress one.
    cy, cq = ref.year, _quarter_of(ref.month)
    # last complete quarter:
    y, q = cy, cq - 1
    if q == 0:
        y, q = cy - 1, 4
    out: list[tuple[int, int]] = []
    for _ in range(count):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    out.reverse()  # oldest first
    return out


def _sheet_name_for_array(name: str, used: set[str]) -> str:
    """Excel sheet names: max 31 chars, no /\\?*[]"""
    bad = '/\\?*[]:'
    clean = "".join(c for c in (name or "Array") if c not in bad).strip()
    if not clean:
        clean = "Array"
    base = clean[:31]
    final = base
    i = 2
    while final in used:
        suffix = f" {i}"
        final = (base[:31 - len(suffix)] + suffix)
        i += 1
    used.add(final)
    return final


# ── main builder ─────────────────────────────────────────────────────
def build_workbook(tenant_id: Optional[str] = None,
                   year: Optional[int] = None,
                   out_path: Optional[pathlib.Path] = None,
                   *, quarters: int = 6,
                   reference_date: Optional[date] = None,
                   client_id: Optional[int] = None) -> pathlib.Path:
    """Generate the GMCS-format workbook for ONE client. Returns saved path.

    Calling conventions:
      build_workbook(client_id=N, ...)         -- preferred, post-Phase-1
      build_workbook(tenant_id="ten_…", ...)   -- legacy fallback: picks the
                                                  first Client under that
                                                  tenant; preserves callers
                                                  that haven't migrated yet.

    Exactly one of `client_id` or `tenant_id` must be provided.

    `year` is accepted for API back-compat with the legacy month-grid writer
    but the GMCS format is rolling-quarter based; it's only used to default
    the output filename.

    `quarters` is the number of trailing complete quarters to include
    (default 6 = 18 months, matching Bruce's GMCS workbook).
    """
    if client_id is None and tenant_id is None:
        raise ValueError("build_workbook requires client_id or tenant_id")

    ref = reference_date or date.today()
    qlist = _rolling_quarters(ref, count=quarters)
    last_y, last_q = qlist[-1]
    if year is None:
        year = last_y

    with SessionLocal() as db:
        # Resolve client_id from tenant_id when legacy mode used
        if client_id is None:
            client = db.execute(
                select(Client).where(Client.tenant_id == tenant_id)
                              .order_by(Client.id.asc())
            ).scalars().first()
            if client is None:
                raise ValueError(
                    f"Tenant {tenant_id} has no Client rows; run migrations.")
            client_id = client.id
        else:
            client = db.get(Client, client_id)
            if client is None:
                raise ValueError(f"unknown client {client_id}")

        tenant = db.get(Tenant, client.tenant_id)
        if not tenant:
            raise ValueError(f"unknown tenant {client.tenant_id}")

        if out_path is None:
            out_path = (REPORTS_DIR / tenant.id / f"client-{client.id}"
                        / f"{last_y}-Q{last_q}-GMCS-report.xlsx")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Arrays scoped to THIS client only; skip excluded (below-REC-threshold) arrays.
        arrays = db.execute(
            select(Array).where(
                Array.client_id == client.id,
                Array.excluded.is_(False),
            )
        ).scalars().all()
        arrays_by_id = {a.id: a for a in arrays}
        array_ids = list(arrays_by_id.keys())

        # Accounts under those arrays
        if array_ids:
            accounts = db.execute(
                select(UtilityAccount).where(UtilityAccount.array_id.in_(array_ids))
            ).scalars().all()
        else:
            accounts = []

        def group_for(acc: UtilityAccount) -> tuple[str, Optional[Array]]:
            if acc.array_id and acc.array_id in arrays_by_id:
                a = arrays_by_id[acc.array_id]
                return a.name, a
            return (acc.nickname or acc.account_number), None

        group_of: dict[int, str] = {}
        group_meta: dict[str, Optional[Array]] = {}
        for acc in accounts:
            name, a = group_for(acc)
            group_of[acc.id] = name
            group_meta.setdefault(name, a)

        # Pull bills for those accounts only
        if accounts:
            account_ids = [a.id for a in accounts]
            bills = db.execute(
                select(Bill).where(Bill.account_id.in_(account_ids))
            ).scalars().all()
        else:
            bills = []

        # Per-group kWh by (year, month)
        per_group: dict[str, dict[tuple[int, int], float]] = defaultdict(
            lambda: defaultdict(float))
        for b in bills:
            if not b.kwh_generated or b.kwh_generated <= 0:
                continue
            t = _bill_target_month(b)
            if t is None:
                continue
            grp = group_of.get(b.account_id)
            if not grp:
                continue
            per_group[grp][t] += b.kwh_generated

        groups = sorted(per_group.keys()) or sorted(set(group_of.values()))

    # ── Build workbook ──────────────────────────────────────────────
    wb = Workbook()
    # remove default sheet at end
    default_sheet = wb.active

    TITLE_FONT = Font(bold=True, size=14, color="1F4E2A")
    HDR_FONT = Font(bold=True, size=14, color="FFFFFF")
    HDR_FILL = PatternFill("solid", fgColor="2E6B3A")
    QUARTER_FONT = Font(bold=True, size=11, color="1F4E2A")
    FOOTNOTE_FONT = Font(italic=True, size=9, color="666666")
    BORDER = Border(*[Side(style="thin", color="C8D4C4")] * 4)

    used_names: set[str] = set()

    if not groups:
        # produce an empty stub sheet so the file is never blank
        groups = ["(no data)"]

    for grp_idx, grp in enumerate(groups):
        sheet_title = _sheet_name_for_array(grp, used_names)
        if grp_idx == 0:
            sh = default_sheet
            sh.title = sheet_title
        else:
            sh = wb.create_sheet(title=sheet_title)

        # ── Title (A1 merged A1:C1) ──
        arr = group_meta.get(grp)
        nepool_id = getattr(arr, "nepool_gis_id", None) if arr else None
        title = f"{grp} ({nepool_id})" if nepool_id else grp
        sh["A1"] = title
        sh["A1"].font = TITLE_FONT
        sh["A1"].alignment = Alignment(horizontal="left", vertical="center")
        sh.merge_cells("A1:C1")

        # Row 5 header
        for col, label in enumerate(
            ["Quarter", "Generation (MWh)", "Reporting Amount", "RECs†"],
            start=1,
        ):
            c = sh.cell(5, col, label)
            c.font = HDR_FONT
            c.fill = HDR_FILL
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = BORDER

        # Data: 6 quarter blocks × 3 month rows
        # First block starts at row 7; gap row between blocks (row 6, 10, 14, ...)
        row = 7
        gen_by_month = per_group.get(grp, {})
        for (qy, qq) in qlist:
            for i, (my, mm) in enumerate(_quarter_months(qy, qq)):
                if i == 0:
                    qc = sh.cell(row, 1, f"Q{qq} {qy}")
                    qc.font = QUARTER_FONT
                    qc.alignment = Alignment(horizontal="left", vertical="center")
                kwh = gen_by_month.get((my, mm), 0.0)
                mwh = round(kwh / 1000.0, 3)
                recs = int(mwh)  # floor of MWh
                gc = sh.cell(row, 2, mwh if kwh else None)
                gc.number_format = "General"
                gc.alignment = Alignment(horizontal="right")
                rc = sh.cell(row, 3, mwh if kwh else None)
                rc.number_format = "General"
                rc.alignment = Alignment(horizontal="right")
                ec = sh.cell(row, 4, recs if kwh else None)
                ec.number_format = "General"
                ec.alignment = Alignment(horizontal="right")
                row += 1
            row += 1  # gap row between quarter blocks

        # Footnote — verbatim text from Bruce's GMCS.xlsx
        foot_row = row + (31 - row) if row <= 31 else row
        fc = sh.cell(foot_row, 1, FOOTNOTE_TEXT)
        fc.font = FOOTNOTE_FONT
        fc.alignment = Alignment(horizontal="left", vertical="center")
        sh.merge_cells(start_row=foot_row, start_column=1,
                       end_row=foot_row, end_column=4)

        # Column widths — uniform 24 across all four columns for max readability.
        sh.column_dimensions["A"].width = 24.0
        sh.column_dimensions["B"].width = 24.0
        sh.column_dimensions["C"].width = 24.0
        sh.column_dimensions["D"].width = 24.0

    wb.save(out_path)
    return out_path
