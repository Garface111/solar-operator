"""
Billing-report delivery — the shared pipeline used by BOTH the "send now"
endpoint and the scheduler.

deliver_subscription():
  1. rebuild the BillingMatch from the subscription's stored workbook bytes,
  2. generate the chosen attachments (invoice PDF/XLSX + optional summary),
  3. resolve recipients from the send-mode slider (to me / to client / to both),
  4. send one branded Array Operator email via Resend,
  5. stamp last_sent_at / next_send_at / last_invoice_number.

Keeping this here (not in routes) means the scheduler can import it without
pulling FastAPI request machinery, and the two call sites can never drift.
"""
from __future__ import annotations

import base64
import logging
import os
import pathlib
import tempfile
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select

from .matcher import match_billing_workbook, BillingMatch
from . import invoice as invoice_mod
from . import summary as summary_mod

logger = logging.getLogger(__name__)

AO_FROM = os.getenv("MAIL_FROM_AO", os.getenv("MAIL_FROM",
                    "Array Operator <reports@arrayoperator.com>"))


# ─── scheduling helpers ─────────────────────────────────────────────────────

def next_send_at(cadence: str, after: Optional[datetime] = None) -> datetime:
    """The next delivery instant (09:00 UTC, 1st of the next month/quarter)."""
    after = after or datetime.utcnow()
    year, month = after.year, after.month
    if cadence == "quarterly":
        # next quarter-start month among 1,4,7,10
        for m in (1, 4, 7, 10, 13):
            ny, nm = (year, m) if m <= 12 else (year + 1, 1)
            cand = datetime(ny, nm, 1, 9, 0)
            if cand > after:
                return cand
        return datetime(year + 1, 1, 1, 9, 0)
    # monthly (default): the 1st of next month
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(ny, nm, 1, 9, 0)


# ─── attachment generation ──────────────────────────────────────────────────

def build_match(sub) -> BillingMatch:
    """Rebuild a BillingMatch from a subscription.

    Two paths:
      * workbook-driven (the original) — re-parse the stored .xlsx bytes.
      * manual (no workbook) — the operator typed the customer in; synthesize a
        BillingMatch from the typed allocation_pct × the array's period
        generation. Both produce the same BillingMatch shape so every downstream
        consumer (invoice/summary renderers, delivery, drafts) is unchanged.
    """
    if not sub.source_workbook:
        return build_manual_match(sub)
    return match_billing_workbook(bytes(sub.source_workbook), allow_llm=False)


# Default Vermont solar value used to price a manual customer's share when no
# workbook AND no explicit rate supplies a tariff. Mirrors reconciliation's
# VT_DEFAULT_RATE. Kept as the final fallback below the per-customer rate and
# the operator's global default rate.
MANUAL_TARIFF = 0.18398
MANUAL_BILLING_RATE = 0.9

# Default discount when neither the customer nor the operator set one: 10% off
# the net rate (i.e. the customer pays 90% — matches the legacy MANUAL_BILLING_RATE).
DEFAULT_DISCOUNT = 0.10


def resolve_discount_pricing(sub) -> dict:
    """Resolve the discount-model pricing for a customer.

    invoice = produced kWh × net_rate × (1 − discount).

    Precedence per field (customer override → operator global → built-in default):
      net_rate : sub.net_rate_per_kwh → tenant.default_net_rate_per_kwh
                 → legacy sub.rate_per_kwh/global flat rate (treated as a net rate)
                 → MANUAL_TARIFF (VT default)
      discount : sub.discount_pct → tenant.default_discount_pct → DEFAULT_DISCOUNT (0.10)

    Returns a dict: {net_rate, discount_pct, effective_rate, net_source,
    discount_source} where effective_rate = net_rate × (1 − discount).
    """
    from ..db import SessionLocal
    from ..models import Tenant

    sub_net = getattr(sub, "net_rate_per_kwh", None)
    sub_disc = getattr(sub, "discount_pct", None)
    sub_flat = getattr(sub, "rate_per_kwh", None)   # legacy flat $/kWh override

    g_net = g_disc = g_flat = None
    with SessionLocal() as db:
        t = db.get(Tenant, sub.tenant_id)
        if t:
            g_net = getattr(t, "default_net_rate_per_kwh", None)
            g_disc = getattr(t, "default_discount_pct", None)
            g_flat = getattr(t, "default_billing_rate_per_kwh", None)

    # ── net rate ──
    if sub_net is not None and sub_net > 0:
        net_rate, net_source = float(sub_net), "customer"
    elif g_net is not None and g_net > 0:
        net_rate, net_source = float(g_net), "global"
    elif sub_flat is not None and sub_flat > 0:
        # Legacy per-customer flat rate: treat as a net rate with 0 discount.
        net_rate, net_source = float(sub_flat), "legacy_flat_customer"
    elif g_flat is not None and g_flat > 0:
        net_rate, net_source = float(g_flat), "legacy_flat_global"
    else:
        net_rate, net_source = MANUAL_TARIFF, "vt_default"

    # ── discount ──
    if sub_disc is not None and 0 <= sub_disc < 1:
        discount, discount_source = float(sub_disc), "customer"
    elif g_disc is not None and 0 <= g_disc < 1:
        discount, discount_source = float(g_disc), "global"
    elif net_source in ("legacy_flat_customer", "legacy_flat_global"):
        # A legacy flat rate already encodes the price; don't re-discount it.
        discount, discount_source = 0.0, "legacy_flat"
    else:
        discount, discount_source = DEFAULT_DISCOUNT, "default"

    effective_rate = round(net_rate * (1 - discount), 6)
    return {
        "net_rate": net_rate,
        "discount_pct": discount,
        "effective_rate": effective_rate,
        "net_source": net_source,
        "discount_source": discount_source,
    }


def resolve_rate_per_kwh(sub) -> tuple[Optional[float], str]:
    """DEPRECATED — superseded by resolve_discount_pricing(). Kept for any
    external caller; returns the effective $/kWh under the discount model."""
    p = resolve_discount_pricing(sub)
    src = "customer" if p["discount_source"] == "customer" or p["net_source"] == "customer" else \
          ("global" if "global" in (p["net_source"], p["discount_source"]) else "vt_default")
    return p["effective_rate"], src


def _array_period_kwh(db, array_id: int) -> tuple[Optional[float], Optional[date], Optional[date], Optional[str]]:
    """The array's most recent full-month generation: (array_kwh, start, end,
    month_label). Prefers DailyGeneration; falls back to Bill.kwh_generated.
    Returns (None, None, None, None) when the array has no data yet."""
    from ..models import DailyGeneration, Bill, UtilityAccount

    # Prefer DailyGeneration: pick the latest (year, month) that has rows.
    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh)
        .where(DailyGeneration.array_id == array_id)
        .order_by(DailyGeneration.day.desc())
    ).all()
    if rows:
        latest_day = rows[0].day
        y, m = latest_day.year, latest_day.month
        month_rows = [r for r in rows if r.day.year == y and r.day.month == m]
        total = sum(float(r.kwh or 0) for r in month_rows)
        days = sorted(r.day for r in month_rows)
        label = latest_day.strftime("%Y-%m")
        return round(total, 1), days[0], days[-1], label

    # Fallback: the array's bills (kwh_generated) for the most recent period.
    bill = db.execute(
        select(Bill)
        .join(UtilityAccount, Bill.account_id == UtilityAccount.id)
        .where(UtilityAccount.array_id == array_id,
               Bill.kwh_generated.isnot(None),
               Bill.period_end.isnot(None))
        .order_by(Bill.period_end.desc())
    ).scalars().first()
    if bill is not None:
        ps = bill.period_start.date() if bill.period_start else None
        pe = bill.period_end.date() if bill.period_end else None
        label = pe.strftime("%Y-%m") if pe else None
        return round(float(bill.kwh_generated), 1), ps, pe, label
    return None, None, None, None


def _array_period_kwh_sourced(
    db, array_id: int
) -> tuple[Optional[float], Optional[date], Optional[date], Optional[str], Optional[str]]:
    """Source-agnostic period generation for an array, with provenance.

    Precedence (per Ford's call + the GMP_DAILY_READ_CONTRACT):
      1. GMP daily-read contract (api/reports/gmp_daily_read) — the authoritative
         metered source. We call its functions only; we never touch the gmp_*
         tables/ORM directly (storage stays the data-sponge agent's).
      2. DailyGeneration / Bill (the legacy path) when GMP has no coverage yet
         (e.g. backfill not run / GMP auth blocked).

    Returns (kwh, start, end, label, kwh_source) where kwh_source is one of
    'gmp_api' | 'daily_csv' | None. Mirrors _array_period_kwh's "latest month
    present" semantics so downstream math is unchanged regardless of source.
    """
    # 1) Try the GMP contract first. Defensive: a provisional module / empty
    #    tables must degrade to the fallback, never raise into invoice math.
    try:
        from ..reports import gmp_daily_read as gdr
        months = gdr.get_monthly_totals(array_id, db=db)
        if months:
            latest = months[-1]                       # ascending → last = newest
            y, m = latest["year"], latest["month"]
            series = [r for r in gdr.get_daily_series(array_id, db=db)
                      if r["day"].year == y and r["day"].month == m]
            if series:
                days = sorted(r["day"] for r in series)
                return (round(float(latest["kwh"]), 1), days[0], days[-1],
                        f"{y:04d}-{m:02d}", "gmp_api")
    except Exception:  # noqa: BLE001 — provisional contract / missing tables
        logger.warning("GMP daily-read unavailable for array %s; falling back",
                       array_id, exc_info=True)

    # 2) Legacy fallback: DailyGeneration → Bill.
    kwh, start, end, label = _array_period_kwh(db, array_id)
    return kwh, start, end, label, ("daily_csv" if kwh is not None else None)


def build_manual_match(sub) -> BillingMatch:
    """Synthesize a BillingMatch for a manually-entered customer (no workbook).

    The customer's share = allocation_pct × the array's most recent period
    generation, priced at the default VT solar value. Always returns a matched
    BillingMatch (with a zero-kWh period when the array has no data yet) so the
    demo can render and send; warnings flag any thin data.
    """
    from ..db import SessionLocal
    from ..models import Array
    from .matcher import Period, compute_invoice

    pct = sub.allocation_pct
    warnings: list[str] = []
    array_kwh: Optional[float] = None
    start = end = None
    label: Optional[str] = None
    array_name: Optional[str] = None
    kwh_source: Optional[str] = None

    if sub.array_id is not None:
        with SessionLocal() as db:
            arr = db.get(Array, sub.array_id)
            array_name = arr.name if arr else None
            array_kwh, start, end, label, kwh_source = _array_period_kwh_sourced(
                db, sub.array_id)
    if array_kwh is None:
        warnings.append("No generation data for this array yet — invoice shows $0 "
                        "until production lands.")
        array_kwh = 0.0
    if not pct:
        warnings.append("No allocation % set for this manual customer.")
        pct = 0.0

    customer_kwh = round(array_kwh * pct, 2)
    # Discount pricing: invoice = kWh × net_rate × (1 − discount). Resolve net
    # rate + discount with per-customer override → operator global → defaults
    # (10% off the VT net rate). compute_invoice's billing_rate = (1 − discount),
    # so amount_owed == kWh × net_rate × (1−discount) and solar_savings == the
    # discount the customer receives. Never fabricated — $0 when no generation.
    pricing = resolve_discount_pricing(sub)
    net_rate = pricing["net_rate"]
    discount = pricing["discount_pct"]
    billing_rate = 1.0 - discount
    period = Period(
        month=label, start=start, end=end,
        array_kwh=array_kwh, customer_kwh=customer_kwh,
        tariff=net_rate, adder=0.0,
    )
    computed = compute_invoice(customer_kwh, net_rate, 0.0,
                               billing_rate, "percent_of_array", None)
    computed["invoice_number"] = end.strftime("%Y-%m") if end else label
    computed["period_start"] = start.isoformat() if start else None
    computed["period_end"] = end.isoformat() if end else None
    computed["month"] = label
    computed["project_total_kwh"] = array_kwh
    computed["array_kwh"] = array_kwh
    # Discount-model fields for the UI/invoice (auditable savings story):
    computed["net_rate_per_kwh"] = round(net_rate, 6)
    computed["discount_pct"] = round(discount, 6)
    computed["effective_rate_per_kwh"] = pricing["effective_rate"]
    computed["net_rate_source"] = pricing["net_source"]
    computed["discount_source"] = pricing["discount_source"]
    # Back-compat: keep rate_per_kwh = the effective billed rate + a coarse source.
    computed["rate_per_kwh"] = pricing["effective_rate"]
    computed["rate_source"] = pricing["discount_source"]
    # Where the produced-kWh number came from: 'gmp_api' (authoritative GMP
    # daily-read) | 'daily_csv' (DailyGeneration/Bill fallback) | None (no data).
    computed["kwh_source"] = kwh_source

    return BillingMatch(
        matched=True,
        confidence=1.0,
        source="manual",
        data_sheet=None,
        customer={"name": sub.customer_name, "email": sub.client_email},
        allocation_pct=pct,
        billing_rate=billing_rate,
        billing_model="percent_of_array",
        periods=[period],
        latest_period=period,
        template={"title": "Invoice - Solar Power Generation"},
        computed_invoice=computed,
        project_totals={
            "total_array_kwh": array_kwh,
            "total_customer_kwh": customer_kwh,
            "array_name": array_name,
        },
        warnings=warnings,
    )


def generate_files(match: BillingMatch, formats: list[str], include_summary: bool,
                   out_dir: pathlib.Path, invoice_date: Optional[date] = None,
                   peer: Optional[dict] = None, sub=None) -> list[pathlib.Path]:
    """Render the chosen attachment files into out_dir. Returns their paths.

    When `sub` carries a stored GMP invoice PDF (Paul's dormant hook), it's
    written out and appended so it rides the same email. `sub` is optional and
    defaults to None, keeping the signature back-compatible.
    """
    invoice_date = invoice_date or date.today()
    safe = (match.customer.get("name") or "customer").replace(" ", "_").replace("/", "-")
    inv_no = (match.computed_invoice or {}).get("invoice_number") or invoice_date.strftime("%Y-%m")
    stem = f"{safe}_{inv_no}"
    paths: list[pathlib.Path] = []
    fmts = [f.lower() for f in (formats or ["pdf"])]
    if "pdf" in fmts:
        paths.append(invoice_mod.render_invoice_pdf(
            match, out_dir / f"{stem}_invoice.pdf", invoice_date=invoice_date))
    if "xlsx" in fmts:
        paths.append(invoice_mod.render_invoice_xlsx(
            match, out_dir / f"{stem}_invoice.xlsx", invoice_date=invoice_date))
    if include_summary:
        if "pdf" in fmts:
            paths.append(summary_mod.render_summary_pdf(
                match, out_dir / f"{stem}_summary.pdf", peer=peer))
        elif "xlsx" in fmts:
            paths.append(summary_mod.render_summary_xlsx(
                match, out_dir / f"{stem}_summary.xlsx", peer=peer))
    # GMP utility-bill PDF attachment. Two sources, manual takes precedence:
    #   1. sub.gmp_invoice_pdf — a manually uploaded PDF (Paul's fallback).
    #   2. auto-attach — when sub.auto_attach_gmp is on AND no manual PDF, look
    #      up the captured bill PDF for this array+period via the read seam.
    #      Returns nothing until ingestion persists durable bytes (never fabricated).
    if sub is not None and getattr(sub, "gmp_invoice_pdf", None):
        gmp_path = out_dir / f"{safe}_GMP_invoice.pdf"
        gmp_path.write_bytes(bytes(sub.gmp_invoice_pdf))
        paths.append(gmp_path)
    elif sub is not None and getattr(sub, "auto_attach_gmp", False) \
            and getattr(sub, "array_id", None) is not None:
        try:
            from ..reports import gmp_bill_pdf_read as gbp
            ci = match.computed_invoice or {}
            ps = _parse_iso_date(ci.get("period_start"))
            pe = _parse_iso_date(ci.get("period_end"))
            found = gbp.get_bill_pdf_for_period(sub.array_id, ps, pe)
            if found and found.get("bytes"):
                gmp_path = out_dir / f"{safe}_GMP_bill.pdf"
                gmp_path.write_bytes(found["bytes"])
                paths.append(gmp_path)
        except Exception:  # noqa: BLE001 — provisional seam must never break send
            logger.warning("auto-attach GMP bill lookup failed for sub %s",
                           getattr(sub, "id", "?"), exc_info=True)
    return paths


def _parse_iso_date(s) -> Optional[date]:
    """Parse a 'YYYY-MM-DD'(...) string to a date, or None."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


# ─── recipients ─────────────────────────────────────────────────────────────

def resolve_recipients(sub, tenant) -> tuple[list[str], list[str], list[str]]:
    """Return (to, cc, problems) from the send-mode slider.

    to_me     → operator only.
    to_client → client (+ cc_emails).
    to_both   → client primary, operator cc'd (+ cc_emails).
    """
    op = sub.operator_email or getattr(tenant, "contact_email", None)
    client = sub.client_email
    extra = [e.strip() for e in (sub.cc_emails or "").split(",") if e.strip()]
    problems: list[str] = []
    mode = sub.send_mode or "to_me"
    if mode == "to_me":
        to = [op] if op else []
        cc: list[str] = []
        if not op:
            problems.append("No operator email on file.")
    elif mode == "to_client":
        to = [client] if client else []
        cc = extra
        if not client:
            problems.append("Send mode is 'to client' but no client email is set.")
    else:  # to_both
        to = [client] if client else ([op] if op else [])
        cc = ([op] if (op and client) else []) + extra
        if not client:
            problems.append("Send mode is 'to both' but no client email is set.")
    # de-dup, keep order
    seen: set[str] = set()
    to = [x for x in to if x and not (x in seen or seen.add(x))]
    cc = [x for x in cc if x and x not in to and not (x in seen or seen.add(x))]
    return to, cc, problems


# ─── email ──────────────────────────────────────────────────────────────────

def _b64(path: pathlib.Path) -> dict:
    return {"filename": path.name,
            "content": base64.b64encode(path.read_bytes()).decode()}


def _email_html(match: BillingMatch, sub, is_test: bool,
                note: Optional[str] = None) -> tuple[str, str, str]:
    inv = match.computed_invoice or {}
    cust = match.customer.get("name") or sub.customer_name or "your array"
    period = ""
    if inv.get("period_start") and inv.get("period_end"):
        period = f"{inv['period_start']} → {inv['period_end']}"
    amount = inv.get("amount_owed")
    amount_str = f"${amount:,.2f}" if isinstance(amount, (int, float)) else "—"
    from ..email_skin import render_email_skin, render_email_skin_text
    kwh = (inv.get("kwh") or 0)
    test_banner = (
        '<p style="background:rgba(255,180,84,.12);border:1px solid rgba(255,180,84,.35);'
        'color:#ffb454;padding:10px 14px;border-radius:8px;margin:0 0 16px;font-size:13px;">'
        'Test send — this went to you, not the customer.</p>' if is_test else "")
    subject = f"Solar invoice — {cust}" + (f" ({inv.get('invoice_number')})" if inv.get("invoice_number") else "")

    def _row(label, val, strong=False):
        pad = "10px" if strong else "6px"
        valstyle = "font-weight:700;color:#3fd68a;" if strong else ""
        return (f'<tr><td style="padding:{pad} 0;opacity:.65;">{label}</td>'
                f'<td style="padding:{pad} 0;text-align:right;{valstyle}">{val}</td></tr>')

    # The operator's edited note (Paul's "edit a pre-written email"), shown above
    # the figures. Plain text → escaped + newlines to <br> so it renders safely.
    note_html = ""
    note_text = ""
    if note and note.strip():
        import html as _html
        safe = _html.escape(note.strip()).replace("\n", "<br>")
        note_html = (f'<div style="font-size:14px;line-height:1.55;margin:0 0 16px;">'
                     f'{safe}</div>')
        note_text = note.strip() + "\n\n"

    intro_para = ("" if note_html
                  else f"<p>Automatic solar report for <strong>{cust}</strong>.</p>")
    body_html = (
        f"{test_banner}"
        f"{note_html}"
        f"{intro_para}"
        f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin-top:8px;">'
        f'{_row("Period", period or "—")}'
        f'{_row("Generation", f"{kwh:,.0f} kWh")}'
        f'{_row("Amount due", amount_str, strong=True)}'
        f"</table>"
        f'<p style="margin-top:18px;">The full invoice'
        f'{" and performance summary are" if sub.include_summary else " is"} attached.</p>'
    )
    html = render_email_skin(
        preheader=f"Your automatic solar report for {cust} is attached.",
        intro_line=f"Automatic solar report for {cust}",
        body_html=body_html,
        product="array_operator",
    )
    text = render_email_skin_text(
        intro_line=f"Automatic solar report for {cust}",
        body_text=(
            f"{note_text}"
            f"Solar report for {cust}\n\n"
            f"Period: {period or '—'}\n"
            f"Generation: {kwh:,.0f} kWh\n"
            f"Amount due: {amount_str}\n\n"
            f"The full invoice{' and performance summary are' if sub.include_summary else ' is'} attached."
        ),
        product="array_operator",
    )
    return subject, html, text


def deliver_subscription(db, sub, tenant, *, invoice_date: Optional[date] = None,
                         triggered_by: str = "manual", is_test: bool = False,
                         note: Optional[str] = None) -> dict:
    """Generate + email one subscription's report. Stamps schedule fields on
    success. Returns a structured result dict (never raises for the common
    failure cases — surfaces them in the result instead).

    `note` is the operator's edited email body (from an approved draft); when
    present it leads the email above the figure table."""
    from ..notify import _send_via_resend

    try:
        match = build_match(sub)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"workbook unreadable: {e}"}
    if not match.matched or not match.latest_period:
        return {"ok": False, "error": "no current billing period in the stored workbook"}

    # For a real (non-test) send honor the slider; a test always goes to_me.
    if is_test:
        op = sub.operator_email or getattr(tenant, "contact_email", None)
        to, cc, problems = ([op] if op else []), [], (
            [] if op else ["No operator email on file for the test send."])
    else:
        to, cc, problems = resolve_recipients(sub, tenant)
    if not to:
        return {"ok": False, "error": "; ".join(problems) or "no recipients"}

    formats = sub.formats or ["pdf"]
    with tempfile.TemporaryDirectory(prefix="ao-bill-") as tmp:
        try:
            paths = generate_files(match, formats, sub.include_summary,
                                   pathlib.Path(tmp), invoice_date=invoice_date,
                                   sub=sub)
        except Exception as e:  # noqa: BLE001
            logger.exception("billing render failed")
            return {"ok": False, "error": f"render failed: {e}"}
        attachments = [_b64(p) for p in paths]
        subject, html, text = _email_html(match, sub, is_test, note=note)

        from_addr = None
        if getattr(tenant, "send_from_email", None):
            nm = getattr(tenant, "send_from_name", None) or getattr(tenant, "company_name", None)
            from_addr = f'"{nm}" <{tenant.send_from_email}>' if nm else tenant.send_from_email

        ok = _send_via_resend(
            to=to[0] if len(to) == 1 else to, subject=subject, html=html, text=text,
            attachments=attachments, from_addr=from_addr,
            product=getattr(tenant, "product", "array_operator"),
        )

    result = {"ok": bool(ok), "to": to, "cc": cc,
              "attachments": [p.name for p in paths],
              "invoice_number": (match.computed_invoice or {}).get("invoice_number"),
              "amount_owed": (match.computed_invoice or {}).get("amount_owed"),
              "triggered_by": triggered_by, "test": is_test}
    if ok and not is_test:
        now = datetime.utcnow()
        sub.last_sent_at = now
        sub.last_invoice_number = result["invoice_number"]
        sub.next_send_at = next_send_at(sub.cadence, now)
        db.commit()
    if not ok:
        result["error"] = "email send failed (check RESEND_API_KEY / domain)"
    return result


def _operator_review_email(sub, tenant, draft) -> tuple[str, str, str]:
    """The 'a report is ready for your review' note sent to the OPERATOR when a
    scheduled period lands in approval mode. It does NOT contain the invoice —
    it points the operator at their inbox to review, optionally edit, and send."""
    from ..email_skin import render_email_skin, render_email_skin_text
    from ..branding import app_url  # product-aware app origin
    cust = sub.customer_name or "your customer"
    amt = draft.amount_usd
    amt_str = f"${amt:,.2f}" if isinstance(amt, (int, float)) else "—"
    try:
        url = app_url(getattr(tenant, "product", "array_operator")).rstrip("/") + "/#reports"
    except Exception:  # noqa: BLE001
        url = "https://arrayoperator.com/#reports"
    subject = f"Ready to review — {cust} solar report"
    body_html = (
        f"<p>A new solar report for <strong>{cust}</strong> is drafted and waiting "
        f"in your approval inbox.</p>"
        f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin-top:8px;">'
        f'<tr><td style="padding:6px 0;opacity:.65;">Period</td>'
        f'<td style="padding:6px 0;text-align:right;">{draft.period_label or "—"}</td></tr>'
        f'<tr><td style="padding:6px 0;opacity:.65;">Their production</td>'
        f'<td style="padding:6px 0;text-align:right;">{(draft.customer_kwh or 0):,.0f} kWh</td></tr>'
        f'<tr><td style="padding:10px 0;opacity:.65;">Amount</td>'
        f'<td style="padding:10px 0;text-align:right;font-weight:700;color:#3fd68a;">{amt_str}</td></tr>'
        f"</table>"
        f'<p style="margin-top:18px;"><a href="{url}" '
        f'style="background:#16a34a;color:#fff;padding:11px 20px;border-radius:8px;'
        f'text-decoration:none;font-weight:600;display:inline-block;">Review &amp; send</a></p>'
        f'<p style="margin-top:14px;font-size:13px;opacity:.7;">Nothing has been sent to your '
        f"customer. Open the report, edit anything you like, then approve to send it.</p>"
    )
    html = render_email_skin(
        preheader=f"A solar report for {cust} is ready for your review.",
        intro_line=f"Ready to review — {cust}",
        body_html=body_html, product="array_operator")
    text = render_email_skin_text(
        intro_line=f"Ready to review — {cust}",
        body_text=(
            f"A solar report for {cust} is drafted and waiting in your approval inbox.\n\n"
            f"Period: {draft.period_label or '—'}\n"
            f"Their production: {(draft.customer_kwh or 0):,.0f} kWh\n"
            f"Amount: {amt_str}\n\n"
            f"Review, edit, and send it here: {url}\n\n"
            f"Nothing has been sent to your customer yet."
        ),
        product="array_operator")
    return subject, html, text


def draft_subscription(db, sub, tenant, *, triggered_by: str = "scheduled") -> dict:
    """Approval-mode handling for a due scheduled period: create (or reuse) a
    pending ReportDraft from the stored workbook and email the OPERATOR a
    'ready to review' note. The report lands in their inbox — they open it,
    optionally edit, and click Approve & send. Nothing reaches the customer here.

    Stamps next_send_at forward so the same period isn't re-drafted every tick.
    Returns a structured result dict (never raises for common failures).
    """
    from ..models import ReportDraft
    from ..notify import _send_via_resend

    try:
        match = build_match(sub)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"workbook unreadable: {e}"}
    if not match.matched or not match.latest_period:
        return {"ok": False, "error": "no current billing period in the stored workbook"}

    ci = match.computed_invoice or {}
    inv_no = ci.get("invoice_number")
    period_label = None
    if ci.get("period_start") or ci.get("period_end"):
        period_label = f"{ci.get('period_start') or '—'} → {ci.get('period_end') or '—'}"
    cust_kwh = ci.get("kwh")
    pct = match.allocation_pct
    array_total = ci.get("project_total_kwh") or ci.get("array_kwh")
    if array_total is None and cust_kwh is not None and pct:
        array_total = round(cust_kwh / pct, 1)

    # Idempotent per (subscription, invoice period).
    existing = db.execute(
        select(ReportDraft).where(
            ReportDraft.subscription_id == sub.id,
            ReportDraft.status == "pending",
            ReportDraft.invoice_number == inv_no,
        )
    ).scalars().first()
    draft = existing or ReportDraft(
        tenant_id=sub.tenant_id, subscription_id=sub.id,
        customer_name=sub.customer_name, status="pending")
    draft.period_label = period_label
    draft.array_total_kwh = array_total
    draft.allocation_pct = pct
    draft.customer_kwh = cust_kwh
    draft.amount_usd = ci.get("amount_owed")
    draft.invoice_number = inv_no
    if existing is None:
        db.add(draft)
    # Move the schedule forward so we don't re-draft this period next tick.
    now = datetime.utcnow()
    sub.next_send_at = next_send_at(sub.cadence, now)
    db.commit()

    # Notify the operator (best-effort — the draft is created regardless).
    op = sub.operator_email or getattr(tenant, "contact_email", None)
    notified = False
    if op:
        try:
            subject, html, text = _operator_review_email(sub, tenant, draft)
            from_addr = None
            if getattr(tenant, "send_from_email", None):
                nm = getattr(tenant, "send_from_name", None) or getattr(tenant, "company_name", None)
                from_addr = f'"{nm}" <{tenant.send_from_email}>' if nm else tenant.send_from_email
            notified = bool(_send_via_resend(
                to=op, subject=subject, html=html, text=text,
                from_addr=from_addr, product="array_operator"))
        except Exception:  # noqa: BLE001
            notified = False
    return {"ok": True, "drafted": True, "draft_id": draft.id,
            "operator_notified": notified, "to_review": op,
            "triggered_by": triggered_by}
