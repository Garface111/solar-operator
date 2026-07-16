"""NEPOOL Operator report digests — the operator's heads-up + receipt (2026-06).

Two operator-facing emails close the "did my clients' reports go out, and did they
land?" gap (the same gap api/resend_webhook.py was built to surface):

  1. PRE-SEND REVIEW  — 2 days before a cadence batch fires, email the operator
     exactly what WILL be sent and to whom, so they can review/fix before it goes.
     Stateless: runs daily, acts only when a weekly/monthly/quarterly send is
     exactly 2 days out (mirrors scheduler._deliver_clients_with_frequency's own
     client-selection so the preview matches the real run).

  2. DELIVERY RECEIPT — after the batch sends, email the operator what WAS sent to
     whom, with Resend-CONFIRMED delivered / bounced status. Runs ~2h after the
     09:00 UTC sends so the delivery webhooks (api/resend_webhook.py, which stamps
     Client.last_delivered_at / last_bounced_at) have landed. Data-driven off the
     ReportDelivery log, so a missed run self-heals on the next tick.

The scheduler writes one ReportDelivery row per client it processes in a batch
(record_scheduled_batch); the receipt job reads those rows, classifies each by the
client's live delivery health, emails the operator, and stamps receipt_sent_at so
every batch is reported exactly once.

HONESTY (CLAUDE.md): we never claim "delivered" without a Resend delivered event —
a client we sent to but haven't had confirmation for is reported as "awaiting
confirmation", not delivered. Skips/failures are reported plainly with their reason.
"""
from __future__ import annotations

import html as _html
import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select, or_

from ..db import SessionLocal
from ..models import Tenant, Client, ReportDelivery, now
from ..notify import _send_via_resend
from ..email_skin import render_email_skin, render_email_skin_text
from ..branding import brand_name, dashboard_url
from ..report_eligibility import tenant_reports_eligible, tenant_in_reports_world

logger = logging.getLogger(__name__)

# How long after a scheduled send we wait before the receipt, so Resend delivery
# webhooks have time to land. The receipt cron runs at 11:00 UTC (2h after the
# 09:00 sends); this is the floor a row must age past to be receipted.
_RECEIPT_DELAY_MIN = 90
# Clock-skew tolerance when comparing a delivery/bounce event time to our send
# time (both server-side UTC, but the webhook lands in a separate request).
_SKEW = timedelta(minutes=2)
# Cap how many rows we enumerate per bucket in an email (still stamp them all).
_LIST_CAP = 60

_CADENCE_LABEL = {"weekly": "weekly", "monthly": "monthly", "quarterly": "quarterly"}


# ───────────────────────── batch logging (called by scheduler) ─────────────────

def record_scheduled_batch(cadence: str, results: list[dict]) -> int:
    """Persist one ReportDelivery row per client processed in a scheduled batch.

    `results` are the per-client dicts deliver_for_client returns (plus a
    synthesized dict for clients that raised). Maps each to a status the receipt
    can report. Returns the number of rows written. Best-effort: a logging error
    must never break the actual delivery run (the scheduler wraps the call)."""
    if not results:
        return 0
    ts = now()
    rows: list[ReportDelivery] = []
    for r in results:
        ok = bool(r.get("ok"))
        sent = bool(r.get("email_sent"))
        reason = r.get("reason") or r.get("error")
        if ok and sent:
            status = "sent"
        elif r.get("skipped_empty"):
            status = "skipped_empty"
        elif reason and "recipient" in str(reason).lower():
            status = "no_recipient"
        elif reason and "inactive" in str(reason).lower():
            status = "inactive"
        elif ok and not sent:
            status = "send_failed"  # built fine, but Resend returned False
        else:
            status = "failed"
        rows.append(ReportDelivery(
            tenant_id=r.get("tenant") or r.get("tenant_id") or "",
            client_id=r.get("client_id"),
            client_name=(r.get("client_name") or "(unknown client)")[:200],
            recipient=(r.get("recipient") or "")[:400],
            cadence=cadence,
            status=status,
            reason=(str(reason)[:400] if reason else None),
            sent_at=ts,
        ))
    # Drop rows with no tenant (can't route a receipt) but keep the rest.
    rows = [row for row in rows if row.tenant_id]
    if not rows:
        return 0
    with SessionLocal() as db:
        db.add_all(rows)
        db.commit()
    return len(rows)


# ───────────────────────────── shared HTML bits ────────────────────────────────

def _chip(label: str, color: str, bg: str) -> str:
    return (f'<span style="display:inline-block;font-size:12px;font-weight:700;'
            f'color:{color};background:{bg};border-radius:999px;padding:2px 10px;'
            f'white-space:nowrap;">{_html.escape(label)}</span>')


def _row(left_html: str, right_html: str) -> str:
    return (f'<tr><td style="padding:9px 0;border-bottom:1px solid #e5ddd0;'
            f'vertical-align:top;">{left_html}</td>'
            f'<td style="padding:9px 0;border-bottom:1px solid #e5ddd0;'
            f'text-align:right;vertical-align:top;white-space:nowrap;">{right_html}</td></tr>')


def _client_cell(name: str, sub: str | None) -> str:
    out = f'<strong style="color:#2a2520;">{_html.escape(name)}</strong>'
    if sub:
        out += (f'<div style="color:#6b5e55;font-size:13px;margin-top:2px;">'
                f'{_html.escape(sub)}</div>')
    return out


def _section_header(title: str) -> str:
    return (f'<p style="margin:22px 0 4px;font-size:13px;font-weight:700;'
            f'letter-spacing:.02em;text-transform:uppercase;color:#5b5147;">'
            f'{_html.escape(title)}</p>')


# ─────────────────────────────── pre-send review ───────────────────────────────

def _preview_recipient(client: Client, tenant: Tenant) -> tuple[str, str | None]:
    """Best-effort 'who will this go to' for the review, honoring send_mode —
    the same resolution deliver_for_client applies. Returns (recipient, note)."""
    mode = (tenant.send_mode or "").strip() or (
        "to_both" if bool(tenant.cc_on_reports) else "to_client")
    op = (tenant.contact_email or "").strip()
    cemail = (client.contact_email or "").strip()
    if mode == "to_me":
        return op, "sent to you, to forward"
    if cemail:
        return cemail, None
    if bool(tenant.cc_on_reports) and op:
        return op, "no client email on file — sent to you"
    return "", "⚠ no recipient on file — won't send"


def _delivery_health_note(client: Client) -> str | None:
    """Surface only an actionable warning (a prior bounce) for the review."""
    ld = getattr(client, "last_delivered_at", None)
    lb = getattr(client, "last_bounced_at", None)
    if lb and (not ld or lb >= ld):
        reason = getattr(client, "last_bounce_reason", None)
        return f"last send bounced{(' · ' + str(reason)) if reason else ''}"
    return None


def _build_review_email(op_name: str, send_date_str: str,
                        clients: list[dict],
                        product: str | None = None) -> tuple[str, str, str]:
    # Digests render in the TENANT's product skin (NEPOOL light / AO day-blue)
    # and link to the tenant's own dashboard — post-fold, a migrated Array
    # Operator tenant gets AO-branded review/receipt emails, not NEPOOL ones.
    dash = dashboard_url(product) + "/"
    first = _html.escape(str(op_name).split()[0] if op_name else "there")
    n = len(clients)
    will_send = [c for c in clients if c["recipient"] and c["ready"]]
    no_data = [c for c in clients if c["recipient"] and not c["ready"]]
    no_to = [c for c in clients if not c["recipient"]]

    intro = (f"In 2 days ({send_date_str}) your automatic reports go out. "
             f"Here's what's queued and who it's going to — review now and fix "
             f"anything in your dashboard before it sends.")
    preheader = f"{n} report{'s' if n != 1 else ''} queued for {send_date_str} — review before they send."

    rows = []
    for c in sorted(clients, key=lambda x: (0 if not x["recipient"] else (1 if not x["ready"] else 2), x["name"].lower())):
        recipient = c["recipient"] or "—"
        sub_bits = [recipient]
        if c["note"]:
            sub_bits.append(c["note"])
        if c["health"]:
            sub_bits.append(c["health"])
        left = _client_cell(c["name"], " · ".join(sub_bits))
        if not c["recipient"]:
            right = _chip("No recipient", "#b42318", "#fef3f2")
        elif not c["ready"]:
            right = _chip("No data yet", "#b54708", "#fffaeb")
        else:
            right = _chip(f"Ready · {_CADENCE_LABEL.get(c['cadence'], c['cadence'])}", "#067647", "#ecfdf3")
        rows.append(_row(left, right))

    note_html = ""
    if no_data or no_to:
        bits = []
        if no_data:
            bits.append(f"<strong>{len(no_data)}</strong> ha{'s' if len(no_data)==1 else 've'} "
                        f"no generation data yet and will be <strong>skipped</strong> unless data lands")
        if no_to:
            bits.append(f"<strong>{len(no_to)}</strong> ha{'s' if len(no_to)==1 else 've'} "
                        f"no recipient on file and <strong>won't send</strong>")
        note_html = (f'<p style="background:#fffaeb;border:1px solid #fde7c3;border-radius:8px;'
                     f'padding:12px 16px;font-size:14px;color:#7a4f10;margin:18px 0 0;">'
                     f'Heads up — {"; ".join(bits)}. Add the missing piece in your dashboard '
                     f'and it\'ll go out on schedule.</p>')

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>{intro}</p>"
        f'<table style="width:100%;border-collapse:collapse;margin:8px 0 0;">{"".join(rows)}</table>'
        f"{note_html}"
    )

    body_text_lines = [f"Hi {str(op_name).split()[0] if op_name else 'there'},", "",
                       f"In 2 days ({send_date_str}) your automatic reports go out. What's queued:"]
    for c in clients:
        if not c["recipient"]:
            flag = "NO RECIPIENT — won't send"
        elif not c["ready"]:
            flag = "no data yet — will be skipped"
        else:
            flag = f"ready ({c['cadence']})"
        body_text_lines.append(f"  - {c['name']} → {c['recipient'] or '—'}  [{flag}]")
    body_text_lines += ["", f"Review/fix anything before it sends: {dash}", "",
                        f"— {brand_name(product)}"]
    body_text = "\n".join(body_text_lines)

    subject = f"Review: {n} report{'s' if n != 1 else ''} going out {send_date_str}"
    html = render_email_skin(
        preheader=preheader, headline="Reports going out in 2 days",
        intro_line="A heads-up so you can review before they send",
        body_html=body_html,
        cta={"label": "Review in your dashboard", "url": dash},
        product=product)
    text = render_email_skin_text(
        headline="Reports going out in 2 days",
        intro_line="A heads-up so you can review before they send",
        body_text=body_text,
        cta={"label": "Review in your dashboard", "url": dash},
        product=product)
    return subject, html, text


def run_presend_reviews() -> dict:
    """Daily. When a cadence batch is exactly 2 days out, email each operator
    with report clients (any product — data presence, not product, decides)
    what will be sent to whom so they can review it first."""
    target = now() + timedelta(days=2)
    td = target.date()
    cadences: list[str] = []
    if td.weekday() == 0:               # Monday → weekly send
        cadences.append("weekly")
    if td.day == 1:                     # 1st → monthly send
        cadences.append("monthly")
        if td.month in (1, 4, 7, 10):   # 1st of a quarter → quarterly too
            cadences.append("quarterly")
    if not cadences:
        return {"operators": 0, "cadences": [], "skipped": "no scheduled send in 2 days"}

    send_date_str = target.strftime("%A, %B ") + f"{target.day}, {target.year}"

    try:
        from ..writers.gmcs_writer import report_has_data
    except Exception:  # pragma: no cover
        report_has_data = lambda _cid: True  # noqa: E731 — degrade to "assume data"

    per_tenant: dict[str, list[dict]] = defaultdict(list)
    op_info: dict[str, tuple[str, str, str]] = {}
    with SessionLocal() as db:
        for cadence in cadences:
            rows = db.execute(
                select(Client, Tenant)
                .join(Tenant, Client.tenant_id == Tenant.id)
                .where(Client.active == True)  # noqa: E712
                .where(Client.deleted_at.is_(None))
                .where(or_(
                    Client.report_frequency == cadence,
                    (Client.report_frequency.is_(None)) & (Tenant.report_frequency == cadence),
                ))
            ).all()
            for c, t in rows:
                # Data-presence eligibility (THE FOLD): the join above already
                # establishes "this tenant has an active client on this
                # cadence" — the only tenant-level gates left are standing +
                # not-demo. NO product gate: a migrated Array Operator tenant
                # with report clients gets the same pre-send review.
                if not tenant_reports_eligible(t):
                    continue
                recipient, note = _preview_recipient(c, t)
                try:
                    ready = bool(report_has_data(c.id))
                except Exception:
                    ready = False
                per_tenant[t.id].append({
                    "name": c.name, "recipient": recipient, "note": note,
                    "cadence": cadence, "ready": ready,
                    "health": _delivery_health_note(c),
                })
                op_info[t.id] = ((t.contact_email or "").strip(),
                                 t.operator_name or t.company_name or t.name or "there",
                                 getattr(t, "product", "nepool"))

    operators = 0
    for tid, clients in per_tenant.items():
        op_email, op_name, product = op_info.get(tid, ("", "there", "nepool"))
        if not op_email or not clients:
            continue
        try:
            subject, html, text = _build_review_email(op_name, send_date_str,
                                                      clients, product=product)
            if _send_via_resend(to=op_email, subject=subject, html=html,
                                text=text, product=product):
                operators += 1
        except Exception as exc:  # one operator must not stall the rest
            logger.warning("presend review failed for tenant %s: %s", tid, exc)

    logger.info("presend reviews: operators=%d cadences=%s", operators, cadences)
    return {"operators": operators, "cadences": cadences}


# ─────────────────────────────── delivery receipt ──────────────────────────────

def _classify(db, rows: list[ReportDelivery]) -> dict[str, list[dict]]:
    """Bucket each row by live delivery health: delivered / bounced / awaiting /
    not_sent. 'delivered' requires a Resend delivered event at/after our send."""
    buckets: dict[str, list[dict]] = {
        "delivered": [], "bounced": [], "pending": [], "not_sent": []}
    for r in rows:
        item = {"name": r.client_name, "recipient": r.recipient or "",
                "reason": r.reason, "cadence": r.cadence}
        if r.status != "sent":
            buckets["not_sent"].append(item)
            continue
        c = db.get(Client, r.client_id) if r.client_id else None
        ld = getattr(c, "last_delivered_at", None) if c else None
        lb = getattr(c, "last_bounced_at", None) if c else None
        floor = r.sent_at - _SKEW
        if lb and lb >= floor and (not ld or lb >= ld):
            item["reason"] = getattr(c, "last_bounce_reason", None)
            buckets["bounced"].append(item)
        elif ld and ld >= floor:
            buckets["delivered"].append(item)
        else:
            buckets["pending"].append(item)
    return buckets


def _build_receipt_email(op_name: str, sent_date_str: str, cadences: list[str],
                         buckets: dict[str, list[dict]],
                         product: str | None = None) -> tuple[str, str, str]:
    dash = dashboard_url(product) + "/"
    first = _html.escape(str(op_name).split()[0] if op_name else "there")
    delivered = buckets["delivered"]
    bounced = buckets["bounced"]
    pending = buckets["pending"]
    not_sent = buckets["not_sent"]
    attempted = len(delivered) + len(bounced) + len(pending)
    cad_phrase = " & ".join(_CADENCE_LABEL.get(c, c) for c in cadences) or "scheduled"

    if attempted and not bounced and not pending and not not_sent:
        intro = (f"All {len(delivered)} of your {cad_phrase} report"
                 f"{'s' if len(delivered) != 1 else ''} went out and were "
                 f"confirmed delivered on {sent_date_str}.")
    else:
        intro = (f"Your {cad_phrase} reports went out on {sent_date_str}. "
                 f"Here's exactly what was sent and where it landed.")
    preheader = (f"{len(delivered)} delivered"
                 + (f", {len(bounced)} bounced" if bounced else "")
                 + (f", {len(pending)} pending" if pending else "")
                 + (f", {len(not_sent)} not sent" if not_sent else "")
                 + f" — {sent_date_str}")

    def _block(title: str, chip: str, color: str, bg: str, items: list[dict],
               show_reason: bool = False) -> str:
        if not items:
            return ""
        out = _section_header(title)
        out += '<table style="width:100%;border-collapse:collapse;">'
        for it in items[:_LIST_CAP]:
            sub = it["recipient"] or "—"
            if show_reason and it.get("reason"):
                sub = f"{sub} · {it['reason']}"
            out += _row(_client_cell(it["name"], sub), _chip(chip, color, bg))
        if len(items) > _LIST_CAP:
            out += _row(f'<span style="color:#6b5e55;">+{len(items)-_LIST_CAP} more</span>', "")
        out += "</table>"
        return out

    body_html = (
        f"<p>Hi {first},</p>"
        f"<p>{intro}</p>"
        + _block("Delivered", "Delivered", "#067647", "#ecfdf3", delivered)
        + _block("Bounced — needs a fix", "Bounced", "#b42318", "#fef3f2", bounced, show_reason=True)
        + _block("Sent · awaiting confirmation", "Pending", "#475467", "#f2f4f7", pending)
        + _block("Not sent", "Not sent", "#b54708", "#fffaeb", not_sent, show_reason=True)
    )

    text_lines = [f"Hi {str(op_name).split()[0] if op_name else 'there'},", "",
                  f"Your {cad_phrase} reports — {sent_date_str}:", ""]
    for label, items, with_reason in [
            ("DELIVERED", delivered, False), ("BOUNCED", bounced, True),
            ("AWAITING CONFIRMATION", pending, False), ("NOT SENT", not_sent, True)]:
        if not items:
            continue
        text_lines.append(f"{label} ({len(items)}):")
        for it in items[:_LIST_CAP]:
            line = f"  - {it['name']} → {it['recipient'] or '—'}"
            if with_reason and it.get("reason"):
                line += f"  [{it['reason']}]"
            text_lines.append(line)
        text_lines.append("")
    text_lines += [f"Full delivery health in your dashboard: {dash}", "",
                   f"— {brand_name(product)}"]
    body_text = "\n".join(text_lines)

    if bounced or not_sent:
        subject = (f"Reports sent {sent_date_str}: {len(delivered)} delivered, "
                   f"{len(bounced) + len(not_sent)} need a look")
    else:
        subject = f"Reports delivered: {len(delivered)} sent {sent_date_str}"

    html = render_email_skin(
        preheader=preheader, headline="Your reports went out",
        intro_line="A receipt of what was sent and where it landed",
        body_html=body_html,
        cta={"label": "Open your dashboard", "url": dash}, product=product)
    text = render_email_skin_text(
        headline="Your reports went out",
        intro_line="A receipt of what was sent and where it landed",
        body_text=body_text,
        cta={"label": "Open your dashboard", "url": dash}, product=product)
    return subject, html, text


def run_delivery_receipts() -> dict:
    """Email each operator a receipt of the batch we sent on their behalf,
    with Resend-confirmed delivery status. Processes ReportDelivery rows aged past
    the confirmation window; stamps receipt_sent_at on success so each batch is
    reported exactly once (un-stamped on a transient send failure → retried)."""
    cutoff = now() - timedelta(minutes=_RECEIPT_DELAY_MIN)
    with SessionLocal() as db:
        pending = db.execute(
            select(ReportDelivery.id, ReportDelivery.tenant_id)
            .where(ReportDelivery.receipt_sent_at.is_(None),
                   ReportDelivery.sent_at <= cutoff)
            .order_by(ReportDelivery.tenant_id, ReportDelivery.sent_at)
        ).all()
    by_tenant: dict[str, list[int]] = defaultdict(list)
    for rid, tid in pending:
        by_tenant[tid].append(rid)

    operators = 0
    total_rows = 0
    for tid, row_ids in by_tenant.items():
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, tid)
                rows = [r for r in (db.get(ReportDelivery, i) for i in row_ids) if r]
                if not rows:
                    continue
                # Can't / shouldn't route: stamp so we don't reprocess forever.
                # THE FOLD: a MIGRATED Array Operator tenant (generation_
                # reports flag set) gets its receipt like any NEPOOL operator;
                # an unmigrated AO tenant stays stamped-and-skipped exactly as
                # under the old product gate. Demo is stamped-and-skipped too
                # (seed_demo gives the demo tenant Client rows; its batches
                # must never email anyone).
                if (tenant is None or getattr(tenant, "is_demo", False)
                        or not tenant_in_reports_world(tenant)):
                    stamp = now()
                    for r in rows:
                        r.receipt_sent_at = stamp
                    db.commit()
                    continue

                product = getattr(tenant, "product", "nepool")
                op_email = (tenant.contact_email or "").strip()
                op_name = tenant.operator_name or tenant.company_name or tenant.name or "there"
                cadences = sorted({r.cadence for r in rows})
                sent_date_str = rows[0].sent_at.strftime("%A, %B ") + \
                    f"{rows[0].sent_at.day}, {rows[0].sent_at.year}"
                buckets = _classify(db, rows)

                sent_ok = False
                if op_email:
                    subject, html, text = _build_receipt_email(
                        op_name, sent_date_str, cadences, buckets, product=product)
                    sent_ok = _send_via_resend(to=op_email, subject=subject,
                                               html=html, text=text, product=product)

                # Stamp when we sent, or when there's nothing we can do (no email).
                if sent_ok or not op_email:
                    stamp = now()
                    for r in rows:
                        r.receipt_sent_at = stamp
                    db.commit()
                    total_rows += len(rows)
                    if sent_ok:
                        operators += 1
                else:
                    # transient send failure → leave un-stamped, retry next run
                    db.rollback()
        except Exception as exc:  # one operator must not stall the rest
            logger.warning("delivery receipt failed for tenant %s: %s", tid, exc)

    logger.info("delivery receipts: operators=%d rows=%d", operators, total_rows)
    return {"operators": operators, "rows": total_rows}
