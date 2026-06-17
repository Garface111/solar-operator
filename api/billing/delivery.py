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
    """Rebuild a BillingMatch from the subscription's stored workbook bytes."""
    if not sub.source_workbook:
        raise ValueError("subscription has no stored workbook")
    return match_billing_workbook(bytes(sub.source_workbook), allow_llm=False)


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
    # Dormant GMP-invoice hook: attach the utility's PDF when present (null today).
    if sub is not None and getattr(sub, "gmp_invoice_pdf", None):
        gmp_path = out_dir / f"{safe}_GMP_invoice.pdf"
        gmp_path.write_bytes(bytes(sub.gmp_invoice_pdf))
        paths.append(gmp_path)
    return paths


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


def _email_html(match: BillingMatch, sub, is_test: bool) -> tuple[str, str, str]:
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

    body_html = (
        f"{test_banner}"
        f"<p>Automatic solar report for <strong>{cust}</strong>.</p>"
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
                         triggered_by: str = "manual", is_test: bool = False) -> dict:
    """Generate + email one subscription's report. Stamps schedule fields on
    success. Returns a structured result dict (never raises for the common
    failure cases — surfaces them in the result instead)."""
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
        subject, html, text = _email_html(match, sub, is_test)

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
