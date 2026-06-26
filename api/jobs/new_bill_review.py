"""Array Operator — "come review your next bill" trigger (Jun 2026, Ford's ask).

Ford: "whenever GMP gets an update, it triggers an email that sends to [the
operator] that says come review — we've set up your next bill — and then they're
just prompted to come to this page, and everything is in front of them so they
can review the data and change the email a little, then approve & send."

So: when a NEW GMP bill lands for an offtaker, email the OPERATOR (the Array
Operator account holder who reviews + approves — NOT the end-offtaker) a prompt to
come review the auto-prepared draft invoice on the Reports page, then approve &
send it.

This is a DAILY scheduled sweep, decoupled from the cadence schedule
(deliver_billing_reports) so a new bill triggers the review the moment it's
captured, not on the 1st of the month. It reuses the offtaker↔utility-bill source
of truth (delivery._utility_bill_period_kwh — paper-bill kWh ONLY, never
telemetry) and the draft-build path (delivery.build_match), and emails through the
Array Operator LIGHT "day" skin.

DEDUP: BillingReportSubscription.review_emailed_period stores the latest GMP-bill
PERIOD label (the bill's period_end YYYY-MM) already emailed. The sweep fires only
when a NEWER bill period lands, so each new GMP bill = exactly one review email per
offtaker. A drafted-but-not-yet-emailed period self-heals on the next daily tick.

HONESTY (CLAUDE.md): offtaker invoices are computed EXCLUSIVELY from the utility's
paper bill for the bound account; if no bill covers the latest period we don't
email (nothing to review). The email never claims the invoice was sent — it's a
prompt to come review a DRAFT. No mention of AI; operator voice throughout.
"""
from __future__ import annotations

import html as _html
import logging

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant, BillingReportSubscription, ReportDraft

logger = logging.getLogger(__name__)

_TEST_TO = "ford.genereaux@gmail.com"


def _reports_url(tenant) -> str:
    """Product-aware Reports-tab deep link the operator lands on to review."""
    try:
        from ..branding import app_url
        return app_url(getattr(tenant, "product", "array_operator")).rstrip("/") + "/#reports"
    except Exception:  # noqa: BLE001
        return "https://arrayoperator.com/#reports"


def _period_pretty(label: str | None, draft) -> str:
    """Human period string for the email — prefer the draft's own
    period_start → period_end span, fall back to the YYYY-MM bill label."""
    pl = getattr(draft, "period_label", None)
    if pl:
        return str(pl)
    return label or "the latest period"


def _ensure_draft(db, sub, tenant):
    """Create (or reuse) the pending ReportDraft for this subscription's latest
    GMP-bill period — the same snapshot delivery.draft_subscription builds, but
    WITHOUT sending its generic note or bumping next_send_at (the cadence run owns
    the schedule; this trigger is event-driven). Returns the ReportDraft, or None
    if the workbook/bill can't produce a current period.
    """
    from ..billing.delivery import build_match

    try:
        match = build_match(sub)
    except Exception as e:  # noqa: BLE001
        logger.info("new_bill_review: sub %s workbook unreadable: %s", sub.id, e)
        return None
    if not match.matched or not match.latest_period:
        return None

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

    # Idempotent per (subscription, billing PERIOD) — same stable-period keying as
    # delivery.draft_subscription, so a draft the operator already has in their
    # inbox is updated in place, never duplicated.
    existing = None
    if period_label is not None:
        existing = db.execute(
            select(ReportDraft).where(
                ReportDraft.subscription_id == sub.id,
                ReportDraft.status == "pending",
                ReportDraft.period_label == period_label,
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
    db.flush()  # assign draft.id without committing the dedup stamp prematurely
    return draft


def _build_review_email(sub, tenant, draft, bill_label: str | None) -> tuple[str, str, str]:
    """The "come review your next bill" email to the OPERATOR (AO day skin)."""
    from ..email_skin import render_email_skin, render_email_skin_text

    cust = sub.customer_name or "your customer"
    cust_e = _html.escape(cust)
    period = _period_pretty(bill_label, draft)
    period_e = _html.escape(period)
    amt = draft.amount_usd
    amt_str = f"${amt:,.2f}" if isinstance(amt, (int, float)) else "—"
    kwh = draft.customer_kwh
    kwh_str = f"{kwh:,.0f} kWh" if isinstance(kwh, (int, float)) else "—"
    url = _reports_url(tenant)

    subject = f"Your next solar invoice is ready to review — {cust}"
    preheader = (f"A new utility bill landed for {cust}. We've prepared their "
                 f"{period} invoice — come review, then approve & send.")

    body_html = (
        f"<p>A new utility bill just landed for <strong>{cust_e}</strong>, so "
        f"we've prepared their next invoice. Come review the numbers and the "
        f"cover note, tweak anything you like, then approve &amp; send — nothing "
        f"goes to {cust_e} until you do.</p>"
        f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin-top:10px;">'
        f'<tr><td style="padding:7px 0;opacity:.65;">Billing period</td>'
        f'<td style="padding:7px 0;text-align:right;">{period_e}</td></tr>'
        f'<tr><td style="padding:7px 0;opacity:.65;">Their production</td>'
        f'<td style="padding:7px 0;text-align:right;">{kwh_str}</td></tr>'
        f'<tr><td style="padding:10px 0;opacity:.65;">Amount due</td>'
        f'<td style="padding:10px 0;text-align:right;font-weight:700;color:#2563eb;">{amt_str}</td></tr>'
        f"</table>"
        f'<p style="margin-top:16px;font-size:13px;opacity:.7;">The invoice is '
        f"computed straight from the utility bill for this period. Open it to "
        f"check the figures and edit the email before it goes out.</p>"
    )
    cta = {"label": "Review & send", "url": url}

    body_text = (
        f"A new utility bill just landed for {cust}, so we've prepared their next invoice.\n\n"
        f"Billing period: {period}\n"
        f"Their production: {kwh_str}\n"
        f"Amount due: {amt_str}\n\n"
        f"Come review the numbers and the cover note, edit anything you like, then "
        f"approve & send. Nothing goes to {cust} until you do.\n"
    )

    html = render_email_skin(
        preheader=preheader,
        intro_line=f"Ready to review — {cust}",
        body_html=body_html, cta=cta, product="array_operator")
    text = render_email_skin_text(
        intro_line=f"Ready to review — {cust}",
        body_text=body_text, cta=cta, product="array_operator")
    return subject, html, text


def _operator_recipient(sub, tenant) -> str:
    """The OPERATOR (account holder) who reviews + approves — never the offtaker."""
    return (getattr(sub, "operator_email", None)
            or getattr(tenant, "contact_email", None) or "").strip()


def _from_addr(tenant) -> str | None:
    if getattr(tenant, "send_from_email", None):
        nm = getattr(tenant, "send_from_name", None) or getattr(tenant, "company_name", None)
        return f'"{nm}" <{tenant.send_from_email}>' if nm else tenant.send_from_email
    return None


def run_new_bill_reviews(*, dry_run: bool = False, to_override: str | None = None) -> dict:
    """Daily. For every offtaker subscription bound to a GMP account, if a NEWER
    utility-bill period has landed than the one we last review-emailed, ensure a
    draft exists and email the OPERATOR a "come review your next bill" prompt.

    dry_run=True    → build everything and RETURN the rendered emails + recipients
                      WITHOUT sending or stamping the dedup marker (safe preview).
    to_override     → send the real email to this address instead of the operator
                      (a Ford-test send); still stamps dedup so it isn't re-sent.

    Returns a structured result (operators emailed, per-subscription detail, and
    for dry_run the rendered subject/recipient/period/amount for each candidate).
    """
    from ..billing.delivery import _utility_bill_period_kwh
    from ..notify import _send_via_resend

    emailed = 0
    candidates: list[dict] = []
    previews: list[dict] = []

    with SessionLocal() as db:
        rows = db.execute(
            select(BillingReportSubscription, Tenant)
            .join(Tenant, BillingReportSubscription.tenant_id == Tenant.id)
            .where(BillingReportSubscription.enabled == True)  # noqa: E712
            .where(BillingReportSubscription.deleted_at.is_(None))
            .where(BillingReportSubscription.utility_account_id.isnot(None))
        ).all()

        sub_ids = [
            sub.id for (sub, t) in rows
            if (t.active or t.subscription_status in ("comped", "trialing"))
        ]

        for sid in sub_ids:
            sub = db.get(BillingReportSubscription, sid)
            tenant = db.get(Tenant, sub.tenant_id) if sub else None
            if sub is None or tenant is None:
                continue

            # Source of truth: the latest paper-bill period for this GMP account.
            _kwh, _ps, _pe, label = _utility_bill_period_kwh(db, sub.utility_account_id)
            if not label:
                continue  # no settled bill covers a period yet → nothing to review

            already = (sub.review_emailed_period or "").strip()
            if already == label and not to_override:
                # We've already prompted the operator for this exact bill period.
                continue

            draft = _ensure_draft(db, sub, tenant)
            if draft is None:
                continue

            op = _operator_recipient(sub, tenant)
            subject, email_html, email_text = _build_review_email(sub, tenant, draft, label)
            recipient = to_override or op

            cand = {
                "subscription_id": sid, "tenant_id": tenant.id,
                "customer": sub.customer_name, "period": label,
                "draft_period_label": draft.period_label,
                "amount_usd": draft.amount_usd, "customer_kwh": draft.customer_kwh,
                "operator_recipient": op, "recipient": recipient,
                "subject": subject, "already_emailed_period": already or None,
            }
            candidates.append(cand)

            if dry_run:
                # Roll back the flushed draft so a preview never persists anything.
                db.rollback()
                previews.append({**cand, "html": email_html, "text": email_text})
                continue

            if not recipient:
                # No operator on file → can't route. Stamp so we don't churn it
                # every day, but flag it (the draft is still in their inbox).
                sub.review_emailed_period = label
                db.commit()
                cand["sent"] = False
                cand["note"] = "no operator email on file"
                continue

            sent = False
            try:
                sent = bool(_send_via_resend(
                    to=recipient, subject=subject, html=email_html, text=email_text,
                    from_addr=_from_addr(tenant), product="array_operator"))
            except Exception as exc:  # one operator must not stall the rest
                logger.warning("new_bill_review send failed for sub %s: %s", sid, exc)
                sent = False

            cand["sent"] = sent
            if sent:
                # Stamp dedup only on a confirmed send (a transient failure retries
                # next tick). The draft itself is already committed below.
                sub.review_emailed_period = label
                db.commit()
                emailed += 1
            else:
                # Persist the draft regardless (it's ready in the inbox) but leave
                # the dedup marker so we retry the email on the next daily run.
                db.commit()

    result = {"emailed": emailed, "candidates": candidates}
    if dry_run:
        result["dry_run"] = True
        result["previews"] = previews
    logger.info("new_bill_reviews: emailed=%d candidates=%d dry_run=%s",
                emailed, len(candidates), dry_run)
    return result
