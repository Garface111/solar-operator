"""Batch invoice-export for QuickBooks AND Xero (Array Operator).

Anna/Bruce's ask #3: "a spreadsheet of the invoice data that can be imported into
QuickBooks or Xero." QuickBooks and Xero use DIFFERENT import layouts, so this
emits a DIFFERENT file per platform (Ford, 2026-07-01; IIF added 2026-07-07):

  • xero        → Xero's Sales-Invoice import columns (ContactName, EmailAddress,
                  InvoiceNumber, InvoiceDate, DueDate, Description, Quantity,
                  UnitAmount, AccountCode, TaxType).
  • quickbooks  → QuickBooks ONLINE's invoice-import columns (InvoiceNo, Customer,
                  InvoiceDate, DueDate, Item(Product/Service), ItemDescription,
                  ItemQuantity, ItemRate, ItemAmount).
  • iif         → QuickBooks DESKTOP's native .IIF transaction format (Bruce
                  2026-07-07: he runs QuickBooks Desktop, which imports .IIF not
                  the Online CSV). Tab-delimited TRNS/SPL/ENDTRNS blocks — one
                  balanced invoice per offtaker (AR debit + income credit).

Only offtakers with a REAL billable invoice this period are emitted — never a
fabricated $0 row. Dollar figures + dates come from the same build_match /
invoice_for_period path the PDF/XLSX invoices use, so the export never drifts
from what the customer is actually billed. Dates are M/D/YYYY (US); tax type,
account code, income account, memo, and the product/service name are
operator-settable because the exact values depend on the operator's own
QuickBooks/Xero chart of accounts.

`period` (Bruce 2026-07-07): the batch normally drafts each offtaker's LATEST
settled bill; pass a target 'YYYY-MM' (or 'YYYY-Qn' for quarterly offtakers) to
draft THAT period per offtaker instead. Resolution reuses build_match's own
period_label targeting, so an offtaker with no settled bill for the chosen
period is simply skipped — never fabricated (the has_utility_bill gate is
preserved).
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

_QB_ALIASES = {"quickbooks", "qb", "qbo", "quickbooks-online", "quickbooks_online"}
_IIF_ALIASES = {"iif", "quickbooks-desktop", "quickbooks_desktop", "qbd", "qb-desktop"}

# QuickBooks Desktop IIF defaults (Bruce's chart-of-accounts convention). The
# income account reuses the operator's `account_code` param when set (same field
# that feeds Xero's AccountCode), else this default.
IIF_AR_ACCOUNT = "Accounts Receivable"
IIF_INCOME_ACCOUNT = "Solar Credit Income"


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


def _invoice_fields(inv: dict, sub, memo: str = "") -> Optional[dict]:
    """Normalized fields for one billable invoice, or None when there's nothing
    to bill (no amount) — we never emit a fabricated $0 invoice. `memo`, when
    set, overrides the default "Solar credit — {month}" description across all
    three layouts (Bruce 2026-07-07)."""
    budget_on = bool(inv.get("budget_override")) and inv.get("budgeted_amount") is not None
    amount = inv.get("budgeted_amount") if budget_on else inv.get("amount_owed")
    if amount is None or float(amount) == 0.0:
        return None
    month = inv.get("month") or (inv.get("period_end") or "")[:7]
    default_desc = f"Solar credit — {month}" if month else "Solar credit"
    return {
        "customer": inv.get("customer_name") or "Customer",
        "email": (getattr(sub, "client_email", None) or ""),
        "number": str(inv.get("invoice_number") or ""),
        "date": _mdY(inv.get("invoice_date")),
        "due": _mdY(inv.get("due_date")),
        "desc": (memo.strip() or default_desc),
        "amount": round(float(amount), 2),
    }


def _xero_row(f: dict, account_code: str, tax_type: str) -> list:
    return [f["customer"], f["email"], f["number"], f["date"], f["due"],
            f["desc"], 1, f["amount"], account_code or "", tax_type or ""]


def _qb_row(f: dict, item_name: str) -> list:
    return [f["number"], f["customer"], f["date"], f["due"], item_name or "Solar Credit",
            f["desc"], 1, f["amount"], f["amount"]]


# ── QuickBooks Desktop IIF ───────────────────────────────────────────────────
# IIF is tab-delimited. An invoice is one balanced transaction: a TRNS line
# posting the receivable to Accounts Receivable (+amount), a SPL line posting
# the offsetting credit to the income account (−amount), then ENDTRNS. A single
# !-prefixed header block declares the columns once at the top of the file.
IIF_TRNS_HEADER = ["!TRNS", "TRNSTYPE", "DATE", "ACCNT", "NAME", "AMOUNT", "DOCNUM", "MEMO"]
IIF_SPL_HEADER = ["!SPL", "TRNSTYPE", "DATE", "ACCNT", "NAME", "AMOUNT", "DOCNUM", "MEMO"]
IIF_ENDTRNS_HEADER = ["!ENDTRNS"]


def _iif_field(v) -> str:
    """A single IIF cell: strip tabs/newlines so they never break the delimiting."""
    return str(v if v is not None else "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _iif_line(cells: list) -> str:
    return "\t".join(_iif_field(c) for c in cells)


def _iif_blocks(rows: list[dict], income_account: str) -> str:
    """Assemble the full IIF text: one header block, then a balanced
    TRNS/SPL/ENDTRNS block per invoice. `rows` are the normalized `_invoice_fields`
    dicts. Each block's AR debit (+amount) and income credit (−amount) net to
    zero so QuickBooks Desktop accepts the transaction."""
    ar = IIF_AR_ACCOUNT
    inc = income_account or IIF_INCOME_ACCOUNT
    lines = [
        _iif_line(IIF_TRNS_HEADER),
        _iif_line(IIF_SPL_HEADER),
        _iif_line(IIF_ENDTRNS_HEADER),
    ]
    for f in rows:
        amt = f["amount"]
        date_s = f["date"]
        name = f["customer"]
        doc = f["number"]
        memo = f["desc"]
        # TRNS: receivable owed to the operator (positive on Accounts Receivable).
        lines.append(_iif_line(
            ["TRNS", "INVOICE", date_s, ar, name, f"{amt:.2f}", doc, memo]))
        # SPL: the offsetting income credit (negative) — nets the block to zero.
        lines.append(_iif_line(
            ["SPL", "INVOICE", date_s, inc, name, f"{-amt:.2f}", doc, memo]))
        lines.append(_iif_line(["ENDTRNS"]))
    # IIF files are conventionally CRLF-terminated (Windows-native format), with a
    # trailing newline so the last ENDTRNS is a complete record.
    return "\r\n".join(lines) + "\r\n"


def normalize_format(fmt: Optional[str]) -> str:
    """Map an operator-supplied format string to one of the three canonical
    layouts: 'quickbooks' (QBO CSV), 'iif' (QuickBooks Desktop), or 'xero'
    (default)."""
    v = (fmt or "").strip().lower()
    if v in _IIF_ALIASES:
        return "iif"
    if v in _QB_ALIASES:
        return "quickbooks"
    return "xero"


def build_invoice_register(
    db: Session, tenant_id: str, account_code: str = "", fmt: str = "xero",
    tax_type: str = "", item_name: str = "Solar Credit",
    invoice_date: Optional[date] = None, period: Optional[str] = None,
    memo: str = "",
) -> tuple[str, int]:
    """Build the invoice-export file for a tenant's offtaker invoices in the
    QuickBooks Online (CSV), QuickBooks Desktop (IIF), or Xero (CSV) layout.
    Returns (text, row_count). Best-effort per offtaker — one bad subscription
    never sinks the whole export.

    period ("YYYY-MM"/"YYYY-Qn"): target a specific settled bill period per
    offtaker instead of each one's latest. Offtakers with no settled bill for the
    chosen period are skipped (never fabricated). invoice_date defaults to today;
    memo overrides the per-line description when set."""
    fmt = normalize_format(fmt)
    invoice_date = invoice_date or date.today()
    target_period = (period or "").strip() or None
    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None),
            BillingReportSubscription.enabled == True)  # noqa: E712
        .order_by(BillingReportSubscription.customer_name)
    ).scalars().all()

    # Gather the normalized invoice rows first, then serialize per format. (The
    # CSV formats stream row-by-row; IIF needs the whole set to lay out its
    # header + per-invoice blocks.)
    rows: list[dict] = []
    for sub in subs:
        try:
            # period_label targets the chosen historical bill for this offtaker;
            # None keeps the long-standing latest-bill default. build_match returns
            # an UNMATCHED match (no latest_period) when the offtaker has no settled
            # bill for that period, so it's skipped below — never fabricated.
            match = build_match(sub, period_label=target_period)
            if not match.matched or not match.latest_period:
                continue
            # Mirror the send gate (delivery.deliver_subscription): a manual
            # (non-workbook) offtaker with no settled utility bill would be
            # REFUSED a send — its telemetry-derived figure must not land in
            # the operator's accounting export as a receivable either. Caught
            # at 800-offtaker scale: unbound/held offtakers exported non-zero
            # fabricated rows.
            ci = match.computed_invoice or {}
            if (not getattr(sub, "source_workbook", None)
                    and ci.get("has_utility_bill") is not True):
                continue
            inv = invoice_for_period(match, match.latest_period, invoice_date)
            f = _invoice_fields(inv, sub, memo=memo)
        except Exception:
            f = None      # never let one offtaker break the batch
        if f is not None:
            rows.append(f)

    count = len(rows)

    if fmt == "iif":
        return _iif_blocks(rows, account_code), count

    buf = io.StringIO()
    w = csv.writer(buf)
    if fmt == "quickbooks":
        w.writerow(QB_HEADER)
        for f in rows:
            w.writerow(_qb_row(f, item_name))
    else:
        w.writerow(XERO_HEADER)
        for f in rows:
            w.writerow(_xero_row(f, account_code, tax_type))
    return buf.getvalue(), count
