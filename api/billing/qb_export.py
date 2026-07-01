"""Batch invoice-export for QuickBooks AND Xero (Array Operator).

Anna/Bruce's ask #3: "a spreadsheet of the invoice data that can be imported into
QuickBooks or Xero." QuickBooks and Xero use DIFFERENT import layouts, so this
emits a DIFFERENT CSV per platform (Ford, 2026-07-01):

  • xero        → Xero's Sales-Invoice import columns (ContactName, EmailAddress,
                  InvoiceNumber, InvoiceDate, DueDate, Description, Quantity,
                  UnitAmount, AccountCode, TaxType).
  • quickbooks  → QuickBooks Online's invoice-import columns (InvoiceNo, Customer,
                  InvoiceDate, DueDate, Item(Product/Service), ItemDescription,
                  ItemQuantity, ItemRate, ItemAmount).

Only offtakers with a REAL billable invoice this period are emitted — never a
fabricated $0 row. Dollar figures + dates come from the same build_match /
invoice_for_period path the PDF/XLSX invoices use, so the export never drifts
from what the customer is actually billed. Dates are M/D/YYYY (US); tax type,
account code, and the product/service name are operator-settable because the
exact values depend on the operator's own QuickBooks/Xero chart of accounts.
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

XERO_HEADER = ["ContactName", "EmailAddress", "InvoiceNumber", "InvoiceDate",
               "DueDate", "Description", "Quantity", "UnitAmount", "AccountCode",
               "TaxType"]
QB_HEADER = ["InvoiceNo", "Customer", "InvoiceDate", "DueDate",
             "Item(Product/Service)", "ItemDescription", "ItemQuantity",
             "ItemRate", "ItemAmount"]

_QB_ALIASES = {"quickbooks", "qb", "qbo"}


def _mdY(v) -> str:
    """M/D/YYYY, no leading zeros (e.g. 6/30/2026)."""
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


def _invoice_fields(inv: dict, sub) -> Optional[dict]:
    """Normalized fields for one billable invoice, or None when there's nothing
    to bill (no amount) — we never emit a fabricated $0 invoice."""
    budget_on = bool(inv.get("budget_override")) and inv.get("budgeted_amount") is not None
    amount = inv.get("budgeted_amount") if budget_on else inv.get("amount_owed")
    if amount is None or float(amount) == 0.0:
        return None
    month = inv.get("month") or (inv.get("period_end") or "")[:7]
    return {
        "customer": inv.get("customer_name") or "Customer",
        "email": (getattr(sub, "client_email", None) or ""),
        "number": str(inv.get("invoice_number") or ""),
        "date": _mdY(inv.get("invoice_date")),
        "due": _mdY(inv.get("due_date")),
        "desc": f"Solar credit — {month}" if month else "Solar credit",
        "amount": round(float(amount), 2),
    }


def _xero_row(f: dict, account_code: str, tax_type: str) -> list:
    return [f["customer"], f["email"], f["number"], f["date"], f["due"],
            f["desc"], 1, f["amount"], account_code or "", tax_type or ""]


def _qb_row(f: dict, item_name: str) -> list:
    return [f["number"], f["customer"], f["date"], f["due"], item_name or "Solar Credit",
            f["desc"], 1, f["amount"], f["amount"]]


def normalize_format(fmt: Optional[str]) -> str:
    return "quickbooks" if (fmt or "").strip().lower() in _QB_ALIASES else "xero"


def build_invoice_register(
    db: Session, tenant_id: str, account_code: str = "", fmt: str = "xero",
    tax_type: str = "", item_name: str = "Solar Credit",
    invoice_date: Optional[date] = None,
) -> tuple[str, int]:
    """Build the invoice-export CSV for a tenant's current-period offtaker invoices
    in the QuickBooks or Xero layout. Returns (csv_text, row_count). Best-effort
    per offtaker — one bad subscription never sinks the whole export."""
    fmt = normalize_format(fmt)
    invoice_date = invoice_date or date.today()
    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
        .order_by(BillingReportSubscription.customer_name)
    ).scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    if fmt == "quickbooks":
        w.writerow(QB_HEADER)
        row_fn = lambda f: _qb_row(f, item_name)          # noqa: E731
    else:
        w.writerow(XERO_HEADER)
        row_fn = lambda f: _xero_row(f, account_code, tax_type)  # noqa: E731

    count = 0
    for sub in subs:
        try:
            match = build_match(sub)
            if not match.matched or not match.latest_period:
                continue
            inv = invoice_for_period(match, match.latest_period, invoice_date)
            f = _invoice_fields(inv, sub)
        except Exception:
            f = None      # never let one offtaker break the batch
        if f is not None:
            w.writerow(row_fn(f))
            count += 1
    return buf.getvalue(), count
