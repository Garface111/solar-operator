"""Monthly OFFTAKER BILLING SUMMARY — the whole book on one sheet.

Ford / Norwich Technologies (Sep 2026): after a cycle's invoices have gone out,
the operator wants ONE spreadsheet that answers "where do all 250 offtakers
stand" — who they are, their email, what their arrays generated, what we billed
them, and whether they have paid — delivered automatically some days after the
last invoice of that period was sent.

WHY IT READS THE LEDGER'S ROWS, NOT ITS OWN QUERY
This is the transpose of `invoice_ledger` (which is one offtaker across many
periods; this is one period across every offtaker). Both call
`invoice_ledger.list_payment_rows()` for money + payment state, so the monthly
report and the per-offtaker "Download spreadsheet" can never quote different
numbers for the same invoice. Anything that changes there changes here.

PROVENANCE RULES (the report is a record, not an estimate)
  • Billed $ and kWh prefer the values STAMPED on the subscription at send time
    (`last_sent_amount_usd` / `last_sent_customer_kwh`). A regenerated figure
    could drift after a bill is re-captured or an allocation is edited; what we
    invoiced is a historical fact.
  • Payment state always comes from the live OfftakerPayment row, because that
    genuinely does change after the send (a late payment must show as paid).
  • Where a figure is unknown we write nothing. Never a 0 that reads as "billed
    zero" when the truth is "we don't have it".
"""
from __future__ import annotations

import base64
import io
import logging
import re

from sqlalchemy import select
from datetime import date, datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _operator_label(tenant) -> str:
    """The business name for a business-facing sheet: company first, then the
    person, then the account name (see the Tenant docstring — operator_name is
    the human, company_name is the business)."""
    for attr in ("company_name", "operator_name", "name"):
        v = (getattr(tenant, attr, None) or "").strip()
        if v:
            return v
    return "Operator"

# How long after the LAST invoice of a period the summary goes out.
DEFAULT_LAG_DAYS = 15

# Logical field → (header, column width). Order is the sheet's column order.
COLUMNS: list[tuple[str, str, int]] = [
    ("offtaker",       "Offtaker",        34),
    ("email",          "Email",           34),
    ("array",          "Array",           24),
    ("generation_kwh", "Generated kWh",   15),
    ("billed_usd",     "Billed $",        13),
    ("paid",           "Paid?",           10),
    ("paid_date",      "Paid date",       13),
    ("collected_usd",  "Collected $",     13),
    ("invoice_number", "Invoice #",       18),
    ("sent_date",      "Invoice sent",    14),
]


def _period_of(sub) -> Optional[str]:
    """The YYYY-MM this subscription most recently billed, if any."""
    pk = getattr(sub, "last_sent_period_end", None)
    if pk:
        s = str(pk)
        if len(s) >= 7 and s[4] == "-":
            return s[:7]
    sent = getattr(sub, "last_sent_at", None)
    if sent:
        return sent.strftime("%Y-%m")
    return None


def _recipients(sub) -> str:
    """Every address this offtaker's invoice actually went to."""
    parts: list[str] = []
    for attr in ("client_email", "cc_emails"):
        v = (getattr(sub, attr, None) or "").strip()
        if v:
            parts.extend(x.strip() for x in v.replace(";", ",").split(",") if x.strip())
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return ", ".join(out)


def _array_name(db, sub) -> Optional[str]:
    aid = getattr(sub, "array_id", None)
    if not aid:
        return None
    try:
        from ..models import Array
        arr = db.get(Array, aid)
        return arr.name if arr else None
    except Exception:  # noqa: BLE001
        return None


def collect_rows(db, tenant, period_key: str) -> list[dict]:
    """One row per offtaker billed in `period_key` (YYYY-MM), name-sorted.

    An offtaker that was NOT billed this period is omitted rather than shown
    with blanks — the sheet answers "what went out this cycle", and a dormant
    subscription in the list would read as a missed invoice.
    """
    from sqlalchemy import select
    from ..models import BillingReportSubscription
    from . import invoice_ledger

    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant.id,
            BillingReportSubscription.deleted_at.is_(None),
        )
    ).scalars().all()

    rows: list[dict] = []
    for sub in subs:
        if _period_of(sub) != period_key:
            continue
        try:
            rows.append(_row_for(db, sub, period_key, invoice_ledger))
        except Exception:  # noqa: BLE001
            # One malformed subscription must never sink the whole report; the
            # offtaker still appears, flagged, so a gap is visible not silent.
            logger.exception("monthly report: row failed for sub %s", getattr(sub, "id", "?"))
            rows.append({
                "offtaker": getattr(sub, "customer_name", None) or "(unknown)",
                "email": _recipients(sub),
                "paid": "Unknown",
                "_error": True,
            })

    rows.sort(key=lambda r: (r.get("offtaker") or "").lower())
    return rows


def _row_for(db, sub, period_key: str, invoice_ledger) -> dict:
    # Payment state for THIS period, from the same source the per-offtaker
    # ledger uses. Newest-first, so the first match is the live one.
    pay = None
    for p in invoice_ledger.list_payment_rows(db, sub):
        if (p.get("period_label") or "")[:7] == period_key:
            pay = p
            break

    # Billed $: what we stamped at send; the payment row is the fallback for
    # sends that predate the stamp.
    billed = getattr(sub, "last_sent_amount_usd", None)
    if billed is None and pay:
        billed = pay.get("amount_usd")

    # kWh: stamped at send, else recomputed through the ledger's own helper.
    kwh = getattr(sub, "last_sent_customer_kwh", None)
    if kwh is None:
        try:
            kwh = invoice_ledger._generation_for_period(db, sub, period_key)
        except Exception:  # noqa: BLE001
            kwh = None

    sent_at = getattr(sub, "last_sent_at", None)
    paid_at = None
    if pay and pay.get("paid_at"):
        try:
            paid_at = datetime.fromisoformat(str(pay["paid_at"]).rstrip("Z"))
        except (TypeError, ValueError):
            paid_at = None

    if pay:
        paid_label = "Yes" if pay.get("status") == "paid" else (pay.get("status_label") or "No")
    else:
        # No pay-link exists (Connect not connected, or invoiced offline). Say
        # so rather than implying non-payment.
        paid_label = "Not tracked"

    return {
        "offtaker": getattr(sub, "customer_name", None) or "(unnamed)",
        "email": _recipients(sub),
        "array": _array_name(db, sub),
        "generation_kwh": round(float(kwh), 2) if kwh is not None else None,
        "billed_usd": round(float(billed), 2) if billed is not None else None,
        "paid": paid_label,
        "paid_date": paid_at.date() if paid_at else None,
        "collected_usd": (pay or {}).get("collected_usd"),
        "invoice_number": (getattr(sub, "last_invoice_number", None)
                           or (pay or {}).get("invoice_number")),
        "sent_date": sent_at.date() if sent_at else None,
        "_paid": bool(pay and pay.get("status") == "paid"),
    }


def summarize(rows: list[dict]) -> dict:
    def _sum(field: str) -> Optional[float]:
        vals = [r.get(field) for r in rows if isinstance(r.get(field), (int, float))]
        return round(sum(vals), 2) if vals else None

    return {
        "offtaker_count": len(rows),
        "paid_count": sum(1 for r in rows if r.get("_paid")),
        "total_kwh": _sum("generation_kwh"),
        "total_billed_usd": _sum("billed_usd"),
        "total_collected_usd": _sum("collected_usd"),
    }


def build_workbook(tenant, period_key: str, rows: list[dict],
                   summary: Optional[dict] = None) -> bytes:
    """Render the summary as .xlsx — one header block, one row per offtaker,
    one totals row. Readability is the whole point, so: frozen header, real
    number formats (money as money, dates as dates), and autofilter."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    summary = summary or summarize(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "Offtaker summary"

    ink = "FF1F2937"
    rule = Side(style="thin", color="FFD8DEE8")

    # ── title block ────────────────────────────────────────────────────────
    op = _operator_label(tenant)
    ws["A1"] = f"{op} — offtaker billing summary"
    ws["A1"].font = Font(size=14, bold=True, color=ink)
    ws["A2"] = f"Billing period {period_key}"
    ws["A2"].font = Font(size=10, color="FF6B7280")
    ws["A3"] = ("Generated " + datetime.utcnow().strftime("%Y-%m-%d") +
                " · " + _summary_line(summary))
    ws["A3"].font = Font(size=10, color="FF6B7280")

    header_row = 5
    for i, (_f, header, width) in enumerate(COLUMNS, start=1):
        c = ws.cell(row=header_row, column=i, value=header)
        c.font = Font(bold=True, color="FFFFFFFF")
        c.fill = PatternFill("solid", fgColor="FF374151")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = Border(bottom=rule)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[header_row].height = 22

    money = '"$"#,##0.00'
    r = header_row
    for row in rows:
        r += 1
        for i, (field, _h, _w) in enumerate(COLUMNS, start=1):
            c = ws.cell(row=r, column=i, value=row.get(field))
            c.border = Border(bottom=rule)
            if field in ("billed_usd", "collected_usd"):
                c.number_format = money
            elif field == "generation_kwh":
                c.number_format = "#,##0.00"
            elif field in ("paid_date", "sent_date"):
                c.number_format = "yyyy-mm-dd"
                c.alignment = Alignment(horizontal="center")
            elif field == "paid":
                c.alignment = Alignment(horizontal="center")
                val = (row.get("paid") or "")
                if val == "Yes":
                    c.font = Font(color="FF067647", bold=True)
                elif val in ("Awaiting payment", "No"):
                    c.font = Font(color="FFB42318")
                else:
                    c.font = Font(color="FF6B7280")
            if row.get("_error"):
                c.font = Font(color="FFB42318", italic=True)

    # ── totals ─────────────────────────────────────────────────────────────
    if rows:
        r += 1
        tot = ws.cell(row=r, column=1, value="Total")
        tot.font = Font(bold=True, color=ink)
        by_field = {f: i for i, (f, _h, _w) in enumerate(COLUMNS, start=1)}
        for field, val in (
            ("generation_kwh", summary.get("total_kwh")),
            ("billed_usd", summary.get("total_billed_usd")),
            ("collected_usd", summary.get("total_collected_usd")),
        ):
            if val is None:
                continue
            c = ws.cell(row=r, column=by_field[field], value=val)
            c.font = Font(bold=True, color=ink)
            c.number_format = money if field != "generation_kwh" else "#,##0.00"
        c = ws.cell(row=r, column=by_field["paid"],
                    value=f"{summary.get('paid_count', 0)}/{summary.get('offtaker_count', 0)}")
        c.font = Font(bold=True, color=ink)
        c.alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=1).border = Border(top=Side(style="medium", color="FF374151"))

        ws.auto_filter.ref = (f"A{header_row}:"
                              f"{get_column_letter(len(COLUMNS))}{r - 1}")

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _summary_line(s: dict) -> str:
    bits = [f"{s.get('offtaker_count', 0)} offtakers"]
    if s.get("total_billed_usd") is not None:
        bits.append(f"${s['total_billed_usd']:,.2f} billed")
    if s.get("total_collected_usd") is not None:
        bits.append(f"${s['total_collected_usd']:,.2f} collected")
    bits.append(f"{s.get('paid_count', 0)} paid")
    return " · ".join(bits)


# ── scheduling ─────────────────────────────────────────────────────────────

# Max stored length of the recipient list. The binding constraint is NOT the
# settings columns (Tenant.offtaker_report_recipient and
# OfftakerMonthlyReport.recipient are both String(400)) but
# email_archive.EmailArchive.to_email, which is String(300) and records every
# send. Clamp to the narrowest column in the path so an accepted list can never
# truncate mid-address in the audit trail. Validated at the edge, so a too-long
# list is REFUSED with a clear message rather than silently cut.
RECIPIENTS_MAXLEN = 300


def parse_recipients(raw) -> list[str]:
    """Split an operator-entered recipient field into addresses.

    Accepts comma, semicolon, whitespace or newline separated input — people
    paste from a mail client, a spreadsheet cell, or type it by hand, and all
    three produce different separators. De-duplicated case-insensitively while
    preserving the order typed, so the first-listed address stays first.
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[,;\s]+", str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        addr = part.strip().strip("<>").strip()
        if not addr:
            continue
        k = addr.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(addr)
    return out


def format_recipients(addrs) -> str:
    """The canonical stored form: comma-space separated, in order."""
    return ", ".join(parse_recipients(addrs))


def recipients_for(tenant) -> list[str]:
    """Where this tenant's summary goes — the explicit list if set, else the
    account contact. Always a list, possibly empty."""
    explicit = parse_recipients(getattr(tenant, "offtaker_report_recipient", None))
    if explicit:
        return explicit
    return parse_recipients(getattr(tenant, "contact_email", None))


def lag_days_for(tenant) -> int:
    v = getattr(tenant, "offtaker_report_lag_days", None)
    try:
        return max(0, int(v)) if v is not None else DEFAULT_LAG_DAYS
    except (TypeError, ValueError):
        return DEFAULT_LAG_DAYS


def due_period(db, tenant, now: Optional[datetime] = None) -> Optional[dict]:
    """The period whose summary is due, or None.

    Due means: every enabled subscription that billed the period has been sent,
    the newest of those sends is at least `lag_days` old, and no report row for
    that (tenant, period) exists yet. Returns the anchor so the caller can
    record WHY it fired.
    """
    from sqlalchemy import select
    from ..models import BillingReportSubscription, OfftakerMonthlyReport

    now = now or datetime.utcnow()
    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant.id,
            BillingReportSubscription.deleted_at.is_(None),
            BillingReportSubscription.last_sent_at.isnot(None),
        )
    ).scalars().all()
    if not subs:
        return None

    # Group the sends by the period they billed; consider the newest period
    # that has fully aged. Older unsent periods are not resurrected — a report
    # that never fired stays unfired rather than arriving months late.
    by_period: dict[str, datetime] = {}
    for s in subs:
        pk = _period_of(s)
        if not pk:
            continue
        sent = s.last_sent_at
        if pk not in by_period or sent > by_period[pk]:
            by_period[pk] = sent
    if not by_period:
        return None

    lag = lag_days_for(tenant)
    for pk in sorted(by_period, reverse=True):
        anchor = by_period[pk]
        if now < anchor + timedelta(days=lag):
            continue  # still inside the quiet window
        already = db.execute(
            select(OfftakerMonthlyReport).where(
                OfftakerMonthlyReport.tenant_id == tenant.id,
                OfftakerMonthlyReport.period_key == pk,
            )
        ).scalars().first()
        # Exactly-once on DELIVERY, not on attempt. A row whose send failed
        # (sent_at NULL, error set) is retryable — otherwise one bad address
        # or a transient mailer error silently costs that month's summary
        # forever, with no operator-visible way to recover it.
        if already is not None and already.sent_at is not None:
            return None  # newest aged period already delivered → nothing due
        return {"period_key": pk, "anchor_sent_at": anchor,
                "due_at": anchor + timedelta(days=lag)}
    return None


def next_due_at(db, tenant) -> Optional[dict]:
    """What the UI shows: when the next summary is expected, and for what."""
    from sqlalchemy import select
    from ..models import BillingReportSubscription, OfftakerMonthlyReport

    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant.id,
            BillingReportSubscription.deleted_at.is_(None),
            BillingReportSubscription.last_sent_at.isnot(None),
        )
    ).scalars().all()
    if not subs:
        return None
    by_period: dict[str, datetime] = {}
    for s in subs:
        pk = _period_of(s)
        if not pk:
            continue
        if pk not in by_period or s.last_sent_at > by_period[pk]:
            by_period[pk] = s.last_sent_at
    if not by_period:
        return None
    pk = max(by_period)
    done = db.execute(
        select(OfftakerMonthlyReport).where(
            OfftakerMonthlyReport.tenant_id == tenant.id,
            OfftakerMonthlyReport.period_key == pk,
        )
    ).scalars().first()
    if done is not None and done.sent_at is not None:
        return {"period_key": pk, "already_sent": True,
                "sent_at": done.sent_at.isoformat() + "Z"}
    lag = lag_days_for(tenant)
    return {"period_key": pk, "already_sent": False,
            "anchor_sent_at": by_period[pk].isoformat() + "Z",
            "due_at": (by_period[pk] + timedelta(days=lag)).isoformat() + "Z",
            "lag_days": lag}


# ── build + send ───────────────────────────────────────────────────────────

def filename_for(tenant, period_key: str) -> str:
    import re
    slug = re.sub(r"[^A-Za-z0-9]+", "-", _operator_label(tenant)).strip("-").lower()
    return f"{slug or 'operator'}-offtaker-summary-{period_key}.xlsx"


def generate(db, tenant, period_key: str) -> dict:
    """Build the workbook for a period without sending or recording anything."""
    rows = collect_rows(db, tenant, period_key)
    summary = summarize(rows)
    return {
        "period_key": period_key,
        "rows": rows,
        "summary": summary,
        "filename": filename_for(tenant, period_key),
        "xlsx": build_workbook(tenant, period_key, rows, summary),
    }


def send_report(db, tenant, period_key: str, *, anchor_sent_at=None,
                trigger: str = "scheduled", recipient: Optional[str] = None) -> dict:
    """Build, email and record one period's summary. Exactly-once per period:
    the unique (tenant, period) index rejects a duplicate even if two workers
    race, and we commit the row BEFORE emailing so a send that crashes midway
    can't be retried into a second email."""
    from sqlalchemy.exc import IntegrityError
    from ..models import OfftakerMonthlyReport
    from ..notify import _send_via_resend

    built = generate(db, tenant, period_key)
    s = built["summary"]
    # `recipient` may be a string (possibly a comma-separated list) or a list;
    # falling back to the tenant's configured recipients, then contact_email.
    to_list = parse_recipients(recipient) if recipient else recipients_for(tenant)
    to = format_recipients(to_list)

    row = OfftakerMonthlyReport(
        tenant_id=tenant.id,
        period_key=period_key,
        anchor_sent_at=anchor_sent_at,
        recipient=to or None,
        trigger=trigger,
        offtaker_count=s["offtaker_count"],
        paid_count=s["paid_count"],
        total_kwh=s["total_kwh"],
        total_billed_usd=s["total_billed_usd"],
        total_collected_usd=s["total_collected_usd"],
        filename=built["filename"],
        xlsx_bytes=built["xlsx"],
    )
    # Reuse a prior attempt that never actually went out, rather than tripping
    # the unique index and refusing forever. A row WITH sent_at is a real
    # delivery and is still untouchable.
    prior = db.execute(
        select(OfftakerMonthlyReport).where(
            OfftakerMonthlyReport.tenant_id == tenant.id,
            OfftakerMonthlyReport.period_key == period_key,
        )
    ).scalars().first()
    if prior is not None:
        if prior.sent_at is not None:
            logger.info("monthly report: %s %s already delivered — not re-sending",
                        tenant.id, period_key)
            return {"ok": False, "duplicate": True, "period_key": period_key}
        # Refresh the failed row in place with this attempt's numbers.
        prior.anchor_sent_at = anchor_sent_at or prior.anchor_sent_at
        prior.recipient = to or None
        prior.trigger = trigger
        prior.offtaker_count = s["offtaker_count"]
        prior.paid_count = s["paid_count"]
        prior.total_kwh = s["total_kwh"]
        prior.total_billed_usd = s["total_billed_usd"]
        prior.total_collected_usd = s["total_collected_usd"]
        prior.filename = built["filename"]
        prior.xlsx_bytes = built["xlsx"]
        prior.error = None
        row = prior
        db.commit()
    else:
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Lost a race with a concurrent worker — it owns this period.
            db.rollback()
            logger.info("monthly report: %s %s claimed concurrently — not re-sending",
                        tenant.id, period_key)
            return {"ok": False, "duplicate": True, "period_key": period_key}

    if not to_list:
        row.error = "no recipient on file"
        db.commit()
        return {"ok": False, "reason": "no_recipient", "period_key": period_key,
                "report_id": row.id}

    subject = f"Offtaker billing summary — {period_key}"
    body = _email_body(tenant, period_key, s, built)
    try:
        ok = _send_via_resend(
            # One email addressed to everyone (not N separate sends): the
            # operator and whoever they added should see the same thread, and
            # the mailer takes a list natively.
            to=(to_list[0] if len(to_list) == 1 else to_list),
            subject=subject, html=body["html"], text=body["text"],
            # Resend wants base64 TEXT, not raw bytes (see notify._send_via_resend
            # callers: every one b64-encodes before handing the dict over).
            attachments=[{
                "filename": built["filename"],
                "content": base64.b64encode(built["xlsx"]).decode(),
            }],
            product="array_operator",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("monthly report: send failed for %s %s", tenant.id, period_key)
        row.error = str(e)[:500]
        db.commit()
        return {"ok": False, "reason": "send_failed", "error": str(e)[:200],
                "period_key": period_key, "report_id": row.id}

    if ok:
        row.sent_at = datetime.utcnow()
    else:
        row.error = "mailer refused the send"
    db.commit()
    return {"ok": bool(ok), "period_key": period_key, "report_id": row.id,
            "recipient": to, **s}


def _email_body(tenant, period_key: str, s: dict, built: Optional[dict] = None) -> dict:
    unpaid = max(0, int(s.get("offtaker_count") or 0) - int(s.get("paid_count") or 0))
    billed = s.get("total_billed_usd")
    collected = s.get("total_collected_usd")
    lines = [
        f"Offtaker billing summary for {period_key}.",
        "",
        f"Offtakers invoiced: {s.get('offtaker_count', 0)}",
    ]
    if billed is not None:
        lines.append(f"Total billed: ${billed:,.2f}")
    if collected is not None:
        lines.append(f"Total collected: ${collected:,.2f}")
    lines.append(f"Paid: {s.get('paid_count', 0)}  ·  Outstanding: {unpaid}")
    lines += ["", "The attached spreadsheet lists every offtaker, their email, "
                  "what their array generated, what they were billed, and "
                  "whether they have paid."]
    text = "\n".join(lines)

    rows_html = "".join(
        f'<tr><td style="padding:4px 14px 4px 0;opacity:.65;">{k}</td>'
        f'<td style="padding:4px 0;font-weight:600;">{v}</td></tr>'
        for k, v in [
            ("Offtakers invoiced", s.get("offtaker_count", 0)),
            ("Total billed", f"${billed:,.2f}" if billed is not None else "—"),
            ("Total collected", f"${collected:,.2f}" if collected is not None else "—"),
            ("Paid", s.get("paid_count", 0)),
            ("Outstanding", unpaid),
        ]
    )
    body_html = f'<table style="border-collapse:collapse;font-size:14px;">{rows_html}</table>'
    html = body_html
    try:
        from ..email_skin import render_email_skin
        html = render_email_skin(
            preheader=(f"{s.get('offtaker_count', 0)} offtakers · "
                       f"{s.get('paid_count', 0)} paid · {unpaid} outstanding"),
            headline=f"Offtaker billing summary — {period_key}",
            intro_line="Where every offtaker stands for this billing period.",
            body_html=body_html,
            footer_line=("Figures are what was invoiced; payment state is live as "
                         "of this morning."),
            attachment_label=(built or {}).get("filename"),
            attachment_size_bytes=len((built or {}).get("xlsx") or b"") or None,
            attachment_caption=("Every offtaker, their email, generation, amount "
                                "billed, and whether they have paid."),
            product="array_operator",
        )
    except Exception:  # noqa: BLE001
        logger.exception("monthly report: email skin failed — sending unskinned")
    return {"html": html, "text": text}


def run_due_reports(now: Optional[datetime] = None) -> dict:
    """Scheduler entry point — one pass over every Array Operator tenant."""
    from sqlalchemy import select
    from ..db import SessionLocal
    from ..models import Tenant

    sent = 0
    checked = 0
    errors = 0
    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(
                Tenant.product == "array_operator",
                Tenant.active.is_(True),
            )
        ).scalars().all()
        for t in tenants:
            checked += 1
            if getattr(t, "offtaker_report_enabled", True) is False:
                continue
            try:
                due = due_period(db, t, now=now)
                if not due:
                    continue
                res = send_report(db, t, due["period_key"],
                                  anchor_sent_at=due["anchor_sent_at"],
                                  trigger="scheduled",
                                  recipient=recipients_for(t))
                if res.get("ok"):
                    sent += 1
                    logger.info("monthly offtaker report sent: %s %s (%s offtakers)",
                                t.id, due["period_key"], res.get("offtaker_count"))
            except Exception:  # noqa: BLE001
                errors += 1
                logger.exception("monthly offtaker report failed for tenant %s", t.id)
                db.rollback()
    return {"checked": checked, "sent": sent, "errors": errors}
