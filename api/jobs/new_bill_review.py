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


def _reports_url(tenant, sub_id=None, review_queue=False) -> str:
    """Product-aware Reports-tab deep link the operator lands on to review.

    With sub_id, deep-links straight to THAT offtaker's draft: the ?draft param
    sits before the #hash so the SPA's hash routing still resolves to #reports,
    and reports.js reads it to open + scroll to + flash that exact review card."""
    try:
        from ..branding import app_url
        base = app_url(getattr(tenant, "product", "array_operator")).rstrip("/")
    except Exception:  # noqa: BLE001
        base = "https://arrayoperator.com"
    if sub_id is not None:
        return f"{base}/?draft={sub_id}#reports"      # one offtaker → open that card
    if review_queue:
        return f"{base}/?review=1#reports"            # a batch → land on the review queue
    return f"{base}/#reports"


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
    url = _reports_url(tenant, sub.id)   # deep-link straight to THIS offtaker's draft

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


def _build_review_digest_email(tenant, items) -> tuple[str, str, str]:
    """ONE "come review" email covering ALL of an operator's offtakers whose new
    bills are ready this run — so an operator with 100 offtakers gets a single
    morning digest, not 100 separate emails (Ford, 2026-07-01). Reads personal for
    a single offtaker; a scannable table + total for many. AO day skin."""
    from ..email_skin import render_email_skin, render_email_skin_text

    n = len(items)

    def _amt(v):
        return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"
    total = sum(i["amount_usd"] for i in items if isinstance(i.get("amount_usd"), (int, float)))

    if n == 1:
        i = items[0]
        url = _reports_url(tenant, i.get("subscription_id"))   # → open THIS offtaker's draft
        cust = i.get("customer") or "your customer"
        cust_e = _html.escape(cust)
        period_e = _html.escape(str(i.get("period") or "the latest period"))
        kwh = i.get("customer_kwh")
        kwh_str = f"{kwh:,.0f} kWh" if isinstance(kwh, (int, float)) else "—"
        subject = f"Your next solar invoice is ready to review — {cust}"
        preheader = (f"A new utility bill landed for {cust}. We've prepared their "
                     f"invoice — come review, then approve & send.")
        body_html = (
            f"<p>A new utility bill just landed for <strong>{cust_e}</strong>, so we've "
            f"prepared their next invoice. Come review the numbers and the cover note, "
            f"tweak anything, then approve &amp; send — nothing goes to {cust_e} until you do.</p>"
            f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin-top:10px;">'
            f'<tr><td style="padding:7px 0;opacity:.65;">Billing period</td>'
            f'<td style="padding:7px 0;text-align:right;">{period_e}</td></tr>'
            f'<tr><td style="padding:7px 0;opacity:.65;">Their production</td>'
            f'<td style="padding:7px 0;text-align:right;">{kwh_str}</td></tr>'
            f'<tr><td style="padding:10px 0;opacity:.65;">Amount due</td>'
            f'<td style="padding:10px 0;text-align:right;font-weight:700;color:#2563eb;">{_amt(i.get("amount_usd"))}</td></tr>'
            f"</table>")
        intro = f"Ready to review — {cust}"
        body_text = (f"A new utility bill just landed for {cust}. We've prepared their next invoice.\n\n"
                     f"Billing period: {i.get('period') or '—'}\n"
                     f"Their production: {kwh_str}\nAmount due: {_amt(i.get('amount_usd'))}\n\n"
                     f"Come review, edit anything, then approve & send. Nothing goes to {cust} until you do.\n")
    else:
        url = _reports_url(tenant, review_queue=True)   # → land on the review queue
        subject = f"{n} solar invoices are ready to review"
        preheader = (f"New utility bills landed for {n} offtakers. We've prepared their "
                     f"invoices — come review them all, then approve & send.")
        rows = ""
        for i in items:
            rows += (f'<tr><td style="padding:6px 0;">{_html.escape(str(i.get("customer") or "—"))}</td>'
                     f'<td style="padding:6px 0;opacity:.6;text-align:right;">{_html.escape(str(i.get("period") or "—"))}</td>'
                     f'<td style="padding:6px 0;text-align:right;font-weight:600;">{_amt(i.get("amount_usd"))}</td></tr>')
        body_html = (
            f"<p>New utility bills just landed for <strong>{n} offtakers</strong>, so we've "
            f"prepared their next invoices. Review them all in one place — tweak anything, then "
            f"approve &amp; send. Nothing goes to any customer until you do.</p>"
            f'<table width="100%" style="font-size:14px;border-collapse:collapse;margin-top:10px;">'
            f'<tr><td style="padding:4px 0;opacity:.5;font-size:12px;">Offtaker</td>'
            f'<td style="padding:4px 0;opacity:.5;font-size:12px;text-align:right;">Period</td>'
            f'<td style="padding:4px 0;opacity:.5;font-size:12px;text-align:right;">Amount</td></tr>'
            f"{rows}"
            f'<tr><td colspan="2" style="padding:9px 0;border-top:1px solid #e5e7eb;font-weight:700;">Total</td>'
            f'<td style="padding:9px 0;border-top:1px solid #e5e7eb;text-align:right;font-weight:700;color:#2563eb;">{_amt(total)}</td></tr>'
            f"</table>")
        intro = f"{n} invoices ready to review"
        body_text = (f"New utility bills landed for {n} offtakers. We've prepared their invoices:\n\n"
                     + "\n".join(f"  - {i.get('customer') or '—'}: {i.get('period') or '—'} — {_amt(i.get('amount_usd'))}"
                                 for i in items)
                     + f"\n\nTotal: {_amt(total)}\n\nReview them all, edit anything, then approve & send:\n{url}\n")

    cta = {"label": (f"Review & send ({n})" if n > 1 else "Review & send"), "url": url}
    html = render_email_skin(preheader=preheader, intro_line=intro, body_html=body_html,
                             cta=cta, product="array_operator")
    text = render_email_skin_text(intro_line=intro, body_text=body_text, cta=cta,
                                  product="array_operator")
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
    """Daily. For every offtaker bound to a GMP account whose latest bill period is
    NEWER than the one we last review-emailed, ensure a draft exists — then email
    the OPERATOR **one digest** covering ALL their ready offtakers. So an operator
    with 100 offtakers gets a SINGLE morning email listing them, not 100 (Ford,
    2026-07-01). Drafts are still per-offtaker; only the notification is batched.

    dry_run=True    → build everything and RETURN the rendered digest email(s) +
                      recipients WITHOUT sending or stamping dedup (safe preview).
    to_override     → route the real digest to this address instead of the operator
                      (a Ford-test send); still stamps dedup so it isn't re-sent.

    Returns {emailed (operators/digests sent), invoices_ready, candidates:[...],
    and for dry_run previews:[{recipient, count, subject, html, text, items}]}.
    """
    from ..billing.delivery import _utility_bill_period_kwh
    from ..notify import _send_via_resend

    candidates: list[dict] = []
    # (tenant_id, recipient) → {"tenant_id", "recipient", "items":[...], "subs":[(sid,label)]}
    groups: dict = {}

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

        # ── PASS 1: per offtaker — dedup, ensure the draft, collect into a
        #    per-(operator) group. No email here.
        for sid in sub_ids:
            sub = db.get(BillingReportSubscription, sid)
            tenant = db.get(Tenant, sub.tenant_id) if sub else None
            if sub is None or tenant is None:
                continue
            _kwh, _ps, _pe, label = _utility_bill_period_kwh(db, sub.utility_account_id)
            if not label:
                continue  # no settled bill covers a period yet → nothing to review
            already = (sub.review_emailed_period or "").strip()
            if already == label and not to_override:
                continue  # already prompted for this exact bill period
            draft = _ensure_draft(db, sub, tenant)
            if draft is None:
                continue
            op = _operator_recipient(sub, tenant)
            recipient = to_override or op
            item = {
                "subscription_id": sid, "tenant_id": tenant.id,
                "customer": sub.customer_name, "period": label,
                "draft_period_label": draft.period_label,
                "amount_usd": draft.amount_usd, "customer_kwh": draft.customer_kwh,
                "operator_recipient": op, "recipient": recipient,
                "already_emailed_period": already or None,
            }
            candidates.append(item)
            key = (tenant.id, recipient or "")
            g = groups.setdefault(key, {"tenant_id": tenant.id, "recipient": recipient,
                                        "items": [], "subs": []})
            g["items"].append(item)
            g["subs"].append((sid, label))
            if dry_run:
                db.rollback()   # a preview never persists a flushed draft
            else:
                db.commit()     # the draft is ready in the inbox regardless of the email

        # ── PASS 2: ONE email per (operator) group — the digest of all their
        #    ready offtakers. Stamp dedup on ALL of the group's subs on success.
        emailed = 0
        previews: list[dict] = []
        for g in groups.values():
            items = g["items"]
            recipient = g["recipient"]
            tenant = db.get(Tenant, g["tenant_id"])
            subject, email_html, email_text = _build_review_digest_email(tenant, items)

            if dry_run:
                previews.append({"tenant_id": g["tenant_id"], "recipient": recipient,
                                 "count": len(items), "subject": subject,
                                 "html": email_html, "text": email_text, "items": items})
                continue

            if not recipient:
                # No operator on file → stamp so we don't churn daily (drafts still
                # sit in their inbox for whenever an email is set).
                for (sid, label) in g["subs"]:
                    s = db.get(BillingReportSubscription, sid)
                    if s:
                        s.review_emailed_period = label
                db.commit()
                continue

            sent = False
            try:
                sent = bool(_send_via_resend(
                    to=recipient, subject=subject, html=email_html, text=email_text,
                    from_addr=_from_addr(tenant), product="array_operator"))
            except Exception as exc:  # one operator must not stall the rest
                logger.warning("new_bill_review digest send failed for tenant %s: %s",
                               g["tenant_id"], exc)
                sent = False

            if sent:
                emailed += 1
                for (sid, label) in g["subs"]:
                    s = db.get(BillingReportSubscription, sid)
                    if s:
                        s.review_emailed_period = label
                db.commit()
            # not sent → leave dedup unset so the next daily tick retries (the
            # drafts are already persisted from pass 1).

    result = {"emailed": emailed, "operators_emailed": emailed,
              "invoices_ready": len(candidates), "candidates": candidates}
    if dry_run:
        result["dry_run"] = True
        result["previews"] = previews
    logger.info("new_bill_reviews: operators_emailed=%d invoices_ready=%d dry_run=%s",
                emailed, len(candidates), dry_run)
    return result
