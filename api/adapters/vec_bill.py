"""
VEC / NISC-SmartHub bill-PDF parser → a settled net-meter ``Bill`` row.

WHY THIS EXISTS
---------------
VEC (Vermont Electric Coop) runs on NISC SmartHub. SmartHub's JSON/usage APIs
return ``totalUsage: 0`` for a net-metering "credit" account — the generation
and the net-meter credit rate live ONLY on the printed bill PDF. So a VEC
offtaker can't be priced from the API the way GMP is.

BUT a real VEC bill PDF is GMP-SHAPED on the one line that matters:

    NM Credit  -10,200 kWh @ 0.181160  $1,847.83

That single line carries (a) the generation/excess sent to grid, (b) the bill's
OWN net-meter credit rate, and (c) the $ credit. Parse it into a settled ``Bill``
(``kwh_sent_to_grid`` + ``solar_credit_usd``) and the EXISTING GMP offtaker-credit
path (rate_schedule.resolve_offtaker_excess_credit → delivery.build_manual_match)
prices the offtaker invoice automatically — excess kWh × the bill's own rate — no
operator-entered rate needed. That turns a VEC offtaker from "model A" (measured
generation × an operator rate) into the same bill-priced flow as GMP whenever a
real bill PDF has been uploaded.

The text-parsing core (``parse_vec_bill_text``) is split from the PDF wrapper so it
is testable without a PDF. The Bill upsert is split out (``_upsert_vec_bill``) so it
is testable with a parsed dict and no PDF.

GROUNDED against the real VEC bill for account 6578300 (West Glover Roaring Brook
Solar): kwh 10200, rate 0.18116, credit 1847.83, period 2026-05-21 → 2026-06-21,
bill_date 2026-06-24.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, time

from .smarthub import is_smarthub_provider


def _d(s: str) -> date:
    mm, dd, yy = s.split("/")
    return date(int(yy), int(mm), int(dd))


def parse_vec_bill_text(text: str) -> dict | None:
    """Parse VEC/SmartHub net-meter bill text → settled-bill fields, or None if it
    isn't a parseable net-meter bill. The 'NM Credit <kWh> kWh @ <rate> $<credit>'
    line carries generation + the bill's own credit rate + the $ credit."""
    m = re.search(
        r"NM\s*Credit\s+-?([\d,]+)\s*kWh\s*@\s*([\d.]+)\s+\$?\s*([\d,]+\.\d{2})",
        text, re.I,
    )
    if not m:
        return None
    kwh = int(m.group(1).replace(",", ""))
    rate = float(m.group(2))
    credit = float(m.group(3).replace(",", ""))
    if kwh <= 0:
        return None
    pm = re.search(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})", text)
    ps = _d(pm.group(1)) if pm else None
    pe = _d(pm.group(2)) if pm else None
    bd = re.search(r"Billing Date:?\s*(\d{2}/\d{2}/\d{4})", text, re.I)
    bill_date = _d(bd.group(1)) if bd else pe
    ac = re.search(r"Account\s*(?:#|Number)\s*:?\s*(\d{4,})", text, re.I)
    return {
        "account_number": ac.group(1) if ac else None,
        "kwh_generated": kwh, "kwh_sent_to_grid": kwh,
        "solar_credit_usd": round(credit, 2), "credit_rate": rate,
        "period_start": ps, "period_end": pe, "bill_date": bill_date,
        "is_net_metered": True,
    }


def parse_vec_bill_pdf(pdf_bytes: bytes) -> dict | None:
    """Extract text from a VEC bill PDF and parse it. None if it can't be read."""
    import pdfplumber  # already a dependency

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return None
    return parse_vec_bill_text(text)


def _dt(d: date | None) -> datetime | None:
    """A date → midnight datetime (the Bill columns are DateTime)."""
    return datetime.combine(d, time.min) if d is not None else None


def _upsert_vec_bill(db, ua, parsed: dict):
    """Idempotent climb-only upsert of a settled VEC Bill for utility-account ``ua``
    from a ``parse_vec_bill_text`` dict. Keyed on (account_id, period_end) so one
    Bill exists per billing period; a new period creates a new Bill. Mirrors the GMP
    climb-only convention in array_owners._persist_meter_accounts — never LOWERS an
    existing kwh_generated / solar_credit_usd. ``db.add``-only; the caller commits.

    Returns the Bill instance (added or updated)."""
    from ..models import Bill

    pe = parsed.get("period_end")
    ps = parsed.get("period_start")
    pe_dt = _dt(pe)
    ps_dt = _dt(ps)
    bd_dt = _dt(parsed.get("bill_date")) or pe_dt
    kwh = parsed.get("kwh_generated")
    sent = parsed.get("kwh_sent_to_grid")
    credit = parsed.get("solar_credit_usd")
    bdays = ((pe - ps).days if (pe is not None and ps is not None) else None)

    # Match the same billing period by period_end (one Bill per period), like the
    # GMP path. period_end is the stable key; a parsed bill always carries it for a
    # real VEC statement (the meter-period line). If it's somehow missing, fall back
    # to the newest existing bill so we don't spawn duplicates.
    from sqlalchemy import select

    existing = db.execute(
        select(Bill).where(
            Bill.account_id == ua.id,
            Bill.period_end.isnot(None),
        ).order_by(Bill.period_end.desc())
    ).scalars().all()
    bill = None
    if pe is not None:
        bill = next(
            (b for b in existing
             if b.period_end and b.period_end.date() == pe),
            None,
        )

    if bill is None:
        bill = Bill(
            tenant_id=ua.tenant_id, account_id=ua.id,
            period_start=ps_dt, period_end=pe_dt, bill_date=bd_dt,
            billing_days=bdays,
            kwh_generated=(int(round(float(kwh))) if kwh is not None else None),
            kwh_sent_to_grid=(float(sent) if sent is not None else None),
            solar_credit_usd=(float(credit) if credit is not None else None),
            is_net_metered=True,
            parse_status="parsed",
        )
        db.add(bill)
    else:
        # Climb-only: never lower a captured generation / credit figure.
        if kwh is not None:
            newg = int(round(float(kwh)))
            if bill.kwh_generated is None or newg > bill.kwh_generated:
                bill.kwh_generated = newg
        if sent is not None:
            news = float(sent)
            if bill.kwh_sent_to_grid is None or news > bill.kwh_sent_to_grid:
                bill.kwh_sent_to_grid = news
        if credit is not None:
            newc = float(credit)
            if bill.solar_credit_usd is None or newc > bill.solar_credit_usd:
                bill.solar_credit_usd = newc
        if bill.period_start is None and ps_dt is not None:
            bill.period_start = ps_dt
        if bill.bill_date is None and bd_dt is not None:
            bill.bill_date = bd_dt
        if bill.billing_days is None and bdays is not None:
            bill.billing_days = bdays
        bill.is_net_metered = True
        bill.parse_status = "parsed"
        db.add(bill)
    return bill


def ingest_vec_bill_pdf(db, tenant_id: str, utility_account_id: int,
                        pdf_bytes: bytes) -> dict:
    """Parse a VEC bill PDF and upsert a settled Bill for the bound utility account.

    Steps:
      1. Parse the PDF (parse_vec_bill_pdf). If unparseable → {"ok": False, reason}.
      2. Load the UtilityAccount by id + tenant_id; require a SmartHub provider —
         else {"ok": False, reason}.
      3. Climb-only upsert the Bill (_upsert_vec_bill), idempotent on the period.

    The caller commits. Returns {"ok": True, "parsed": {...}, "bill_id": int} on
    success or {"ok": False, "reason": str} on any guard failure.
    """
    from ..models import UtilityAccount

    parsed = parse_vec_bill_pdf(pdf_bytes)
    if parsed is None:
        return {"ok": False,
                "reason": "could not read a net-meter bill from this PDF"}

    ua = db.get(UtilityAccount, utility_account_id)
    if ua is None or ua.tenant_id != tenant_id:
        return {"ok": False, "reason": "utility account not found"}
    if not is_smarthub_provider((ua.provider or "").lower()):
        return {"ok": False, "reason": "not a VEC/SmartHub account"}

    bill = _upsert_vec_bill(db, ua, parsed)
    db.flush()  # populate bill.id without committing (the caller commits)

    return {
        "ok": True,
        "parsed": {
            "account_number": parsed.get("account_number"),
            "kwh_generated": parsed.get("kwh_generated"),
            "kwh_sent_to_grid": parsed.get("kwh_sent_to_grid"),
            "solar_credit_usd": parsed.get("solar_credit_usd"),
            "credit_rate": parsed.get("credit_rate"),
            "period_start": (parsed["period_start"].isoformat()
                             if parsed.get("period_start") else None),
            "period_end": (parsed["period_end"].isoformat()
                           if parsed.get("period_end") else None),
            "bill_date": (parsed["bill_date"].isoformat()
                          if parsed.get("bill_date") else None),
            "is_net_metered": parsed.get("is_net_metered"),
        },
        "bill_id": bill.id,
    }
