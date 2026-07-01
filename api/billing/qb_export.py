"""QuickBooks / Xero batch invoice-export (Array Operator).

Anna/Bruce's ask #3: "a spreadsheet of the invoice data built that can be imported
into Quickbooks or Xero." Produces a CSV of the current period's offtaker invoices
in the exact column layout of the sample Bruce sent (Norwich Racquet Club's
export, "NRC Invoices April 2026.CSV") so it drops straight into her bookkeeping
import mapping:

    Customer , … , Num , , Date , Due Date , , Description , Qty , Open Balance , <acct>

Only offtakers with a REAL billable invoice this period are emitted — no
fabricated $0 rows. Dollar figures and dates come from the same build_match /
invoice_for_period path the PDF/XLSX invoices use, so the export never drifts
from what the customer is actually billed.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BillingReportSubscription
from .delivery import build_match
from .invoice import invoice_for_period

# Column offsets in the NRC sample (0-indexed). Kept as one map so the layout is
# trivially adjustable if Anna's import expects a different arrangement.
COL_CUSTOMER = 0
COL_NUM = 10
COL_DATE = 12
COL_DUE = 13
COL_DESC = 15
COL_QTY = 16
COL_AMOUNT = 17     # "Open Balance" in the sample header
COL_ACCT = 18       # unlabeled account-code column (e.g. 400/401/402)
_WIDTH = 19


def _mdY(v) -> str:
    """M/D/YYYY with no leading zeros, matching the sample (e.g. 4/2/2026)."""
    if v is None:
        return ""
    d = v
    if isinstance(v, str):
        try:
            d = date.fromisoformat(v[:10])
        except ValueError:
            return v
    try:
        return f"{d.month}/{d.day}/{d.year}"
    except AttributeError:
        return str(v)


def _blank_row() -> list:
    return [""] * _WIDTH


def _header_row() -> list:
    row = _blank_row()
    row[COL_CUSTOMER] = "Customer"
    row[COL_NUM] = "Num"
    row[COL_DATE] = "Date"
    row[COL_DUE] = "Due Date"
    row[COL_DESC] = "Description"
    row[COL_QTY] = "Qty"
    row[COL_AMOUNT] = "Open Balance"
    return row


def _invoice_row(inv: dict, account_code: str) -> Optional[list]:
    """One export line for a built invoice, or None when there's nothing billable
    (no amount) — we never emit a fabricated $0 invoice."""
    budget_on = bool(inv.get("budget_override")) and inv.get("budgeted_amount") is not None
    amount = inv.get("budgeted_amount") if budget_on else inv.get("amount_owed")
    if amount is None or float(amount) == 0.0:
        return None
    month = inv.get("month") or (inv.get("period_end") or "")[:7]
    desc = f"Solar credit — {month}" if month else "Solar credit"
    row = _blank_row()
    row[COL_CUSTOMER] = inv.get("customer_name") or "Customer"
    row[COL_NUM] = str(inv.get("invoice_number") or "")
    row[COL_DATE] = _mdY(inv.get("invoice_date"))
    row[COL_DUE] = _mdY(inv.get("due_date"))
    row[COL_DESC] = desc
    row[COL_QTY] = 1
    row[COL_AMOUNT] = round(float(amount), 2)
    row[COL_ACCT] = account_code or ""
    return row


def build_invoice_register(
    db: Session, tenant_id: str, account_code: str = "",
    invoice_date: Optional[date] = None,
) -> tuple[str, int]:
    """Build the QB/Xero invoice-register CSV for a tenant's current-period
    offtaker invoices. Returns (csv_text, row_count). Best-effort per offtaker —
    one bad subscription never sinks the whole export."""
    invoice_date = invoice_date or date.today()
    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
        .order_by(BillingReportSubscription.customer_name)
    ).scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_header_row())
    count = 0
    for sub in subs:
        try:
            match = build_match(sub)
            if not match.matched or not match.latest_period:
                continue
            inv = invoice_for_period(match, match.latest_period, invoice_date)
            row = _invoice_row(inv, account_code)
        except Exception:
            row = None      # never let one offtaker break the batch
        if row is not None:
            w.writerow(row)
            count += 1
    return buf.getvalue(), count
