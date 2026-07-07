"""Invoice archive / monthly directory (Array Operator) — Anna/Bruce's ask #2.

Bruce: "accumulate all GMP invoices into one master month's directory with
per-array subdirectories where the invoices, and each associated offtaker's bill
and the array's bill would reside" + "the user can drag or download to their
local computer."

Built ON DEMAND from the same stored source the live invoices use (the workbook
/ GMP-bill match + captured Bill.pdf_bytes) — there is no separate invoice store
to drift, migrate, or fall stale. Two surfaces:

  • list_archive()      → a browsable manifest: period → arrays → offtakers, with
                          what's available for each (invoice / offtaker bill /
                          array bill). Feeds the frontend directory view.
  • build_archive_zip() → a single .zip laid out exactly as Bruce described:
                          <month>/<array>/{invoice, each offtaker bill, the
                          array's own bill}.

Best-effort per offtaker — one bad subscription never sinks the whole archive,
and a missing file is honestly omitted, never fabricated.
"""
from __future__ import annotations

import io
import re
import zipfile
import tempfile
import pathlib
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BillingReportSubscription, Array, UtilityAccount, Bill
from .delivery import build_match, generate_files, _normalized_allocations


def _slug(s: Optional[str]) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip()).strip("-") or "item"


def _as_date(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return v.date() if hasattr(v, "date") else v


def _match_period_end(match) -> Optional[date]:
    ci = match.computed_invoice or {}
    return _as_date(ci.get("period_end")) or (
        getattr(match.latest_period, "end", None) if match else None)


def _account_id_for_array(db: Session, array_id: int) -> Optional[int]:
    """The array's OWN utility account (the host meter carrying the group bill)."""
    return db.execute(
        select(UtilityAccount.id).where(UtilityAccount.array_id == array_id)
        .order_by(UtilityAccount.id)
    ).scalars().first()


def _latest_bill_pdf(db: Session, account_id: Optional[int], near: Optional[date]):
    """(pdf_bytes, bill) for the account whose period best matches `near`; the
    latest bill with real PDF bytes otherwise. (None, None) when nothing stored."""
    if account_id is None:
        return None, None
    bills = db.execute(
        select(Bill).where(Bill.account_id == account_id, Bill.pdf_bytes.isnot(None))
        .order_by(Bill.period_end.desc())
    ).scalars().all()
    if not bills:
        return None, None
    chosen = bills[0]
    if near is not None:
        for b in bills:
            be = _as_date(b.period_end)
            try:
                if be and abs((be - near).days) <= 20:
                    chosen = b
                    break
            except TypeError:
                continue
    return bytes(chosen.pdf_bytes), chosen


def _arrays_for_sub(sub) -> list[int]:
    allocs = _normalized_allocations(sub)
    if allocs:
        return [a["array_id"] for a in allocs]
    return [sub.array_id] if getattr(sub, "array_id", None) is not None else []


def _month_label(d: Optional[date]) -> Optional[str]:
    return d.strftime("%Y-%m") if d else None


def _collect(db: Session, tenant_id: str):
    """Shared pass: for each active sub build its match once and gather the facts
    both the manifest and the zip need. Returns a list of per-sub dicts."""
    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
        .order_by(BillingReportSubscription.customer_name)
    ).scalars().all()

    out = []
    for sub in subs:
        try:
            match = build_match(sub)
        except Exception:
            match = None
        billable = bool(match and match.matched and match.latest_period)
        pend = _match_period_end(match) if match else None
        aids = _arrays_for_sub(sub)
        arrays = []
        for aid in aids:
            arr = db.get(Array, aid)
            acct_id = _account_id_for_array(db, aid)
            arrays.append({
                "array_id": aid,
                "array_name": (arr.name if arr else f"Array {aid}"),
                "account_id": acct_id,
            })
        off_acct = getattr(sub, "utility_account_id", None)
        out.append({
            "sub": sub, "match": match, "billable": billable,
            "period_end": pend, "month": _month_label(pend),
            "arrays": arrays, "offtaker_account_id": off_acct,
        })
    return out


def list_archive(db: Session, tenant_id: str) -> dict:
    """Browsable manifest: months → arrays → offtakers, with availability flags."""
    rows = _collect(db, tenant_id)
    months: dict[str, dict] = {}
    for r in rows:
        month = r["month"] or "unscheduled"
        m = months.setdefault(month, {"month": month, "arrays": {}})
        # offtaker bill availability (their own account)
        off_pdf, _ = _latest_bill_pdf(db, r["offtaker_account_id"], r["period_end"])
        for a in (r["arrays"] or [{"array_id": None, "array_name": "Unassigned", "account_id": None}]):
            key = str(a["array_id"])
            arr_entry = m["arrays"].setdefault(key, {
                "array_id": a["array_id"], "array_name": a["array_name"],
                "array_bill_available": False, "offtakers": [],
            })
            arr_pdf, _ = _latest_bill_pdf(db, a["account_id"], r["period_end"])
            arr_entry["array_bill_available"] = arr_entry["array_bill_available"] or bool(arr_pdf)
            arr_entry["offtakers"].append({
                "sub_id": r["sub"].id,
                "customer_name": r["sub"].customer_name,
                "invoice_available": r["billable"],
                "offtaker_bill_available": bool(off_pdf),
            })
    # Order months newest-first; drop the internal dict-keying.
    ordered = []
    for month in sorted(months.keys(), reverse=True):
        m = months[month]
        m["arrays"] = list(m["arrays"].values())
        m["invoice_count"] = sum(
            1 for a in m["arrays"] for o in a["offtakers"] if o["invoice_available"])
        ordered.append(m)
    return {"ok": True, "months": ordered,
            "month_count": len(ordered),
            "latest_month": (ordered[0]["month"] if ordered else None)}


def build_archive_zip(db: Session, tenant_id: str,
                      month: Optional[str] = None) -> tuple[bytes, str, int]:
    """Build the month's archive .zip: <month>/<array>/{invoice, offtaker bill,
    array bill}. Defaults to the latest month present. Returns (bytes, filename,
    file_count)."""
    rows = _collect(db, tenant_id)
    have_months = sorted({r["month"] for r in rows if r["month"]}, reverse=True)
    target = month or (have_months[0] if have_months else None)

    buf = io.BytesIO()
    file_count = 0
    added_array_bill: set[int] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            if target is not None and r["month"] != target:
                continue
            if not r["billable"]:
                continue
            sub = r["sub"]
            month_dir = r["month"] or "unscheduled"
            off_slug = _slug(sub.customer_name)
            # Render this offtaker's invoice (full fidelity, same as a real send).
            inv_bytes = None
            try:
                with tempfile.TemporaryDirectory(prefix="ao-arch-") as tmp:
                    paths = generate_files(r["match"], ["pdf"], False,
                                           pathlib.Path(tmp), sub=sub)
                    inv_path = next((p for p in paths
                                     if str(p).endswith("_invoice.pdf")), None)
                    if inv_path is None and paths:
                        inv_path = paths[0]
                    if inv_path is not None:
                        inv_bytes = pathlib.Path(inv_path).read_bytes()
            except Exception:
                inv_bytes = None
            off_pdf, _ = _latest_bill_pdf(db, r["offtaker_account_id"], r["period_end"])

            arrays = r["arrays"] or [{"array_id": None, "array_name": "Unassigned",
                                      "account_id": None}]
            for a in arrays:
                adir = f"{month_dir}/{_slug(a['array_name'])}"
                if inv_bytes:
                    zf.writestr(f"{adir}/{off_slug}_invoice.pdf", inv_bytes)
                    file_count += 1
                if off_pdf:
                    zf.writestr(f"{adir}/{off_slug}_offtaker-bill.pdf", off_pdf)
                    file_count += 1
                # The array's own bill: once per array folder.
                aid = a["array_id"]
                if aid is not None and aid not in added_array_bill:
                    arr_pdf, _ = _latest_bill_pdf(db, a["account_id"], r["period_end"])
                    if arr_pdf:
                        # Leading "_" keeps this at the top of the folder, above every
                        # offtaker's invoice/bill. Labeled "Master Array Bill" + month.
                        zf.writestr(
                            f"{adir}/_Master-Array-Bill_{_slug(a['array_name'])}_{month_dir}.pdf",
                            arr_pdf)
                        file_count += 1
                    added_array_bill.add(aid)
        if file_count == 0:
            # Honest empty archive: a README, never a silent empty zip.
            zf.writestr(
                f"{target or 'archive'}/README.txt",
                "No billable offtaker invoices for this period yet. Invoices appear "
                "here once a GMP bill with billable excess lands for each offtaker.\n")
    fname = f"offtaker-invoices-{target or date.today().isoformat()}.zip"
    return buf.getvalue(), fname, file_count
