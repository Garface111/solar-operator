"""Mail-room auditor — an independent check of what went out and what is about to.

Two layers, both stored on one OfftakerAuditRun row:

  1. DETERMINISTIC checks over the mail-room board (issued invoices, holds,
     drafts, subscriptions, the GMP cross-check). These run without any model
     and are the floor: duplicate periods, amount jumps, rates nobody entered,
     invoices with no utility bill behind them, over-allocated arrays, bounces,
     unconfirmed sends, unpaid aging, stale drafts and holds, bad recipients.
  2. A CLAUDE review of the same evidence (api/billing/repro/llm.call_json with
     a JSON schema), asked to look for anything a careful billing clerk would
     question that the rules above cannot express. Its findings are labelled
     with the model that produced them and never overwrite the deterministic
     ones.

Only deterministic accepted-dispatch recovery may update local send bookkeeping.
No audit sends email, changes payments or lets model output drive mutations.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

SEVERITIES = ("critical", "high", "medium", "low", "info")
MAX_SENT_FOR_MODEL = 250
MAX_PAYLOAD_CHARS = 140_000


# ── payload ─────────────────────────────────────────────────────────────────

def _slim_sent(x: dict) -> dict:
    keep = ("id", "legacy", "kind", "subscription_id", "customer_name", "invoice_number",
            "status", "sent_at", "period_key", "period_label", "period_start", "period_end",
            "amount_usd", "credit_applied_usd", "kwh", "array_kwh", "allocation_pct",
            "array_share_pct", "rate", "kwh_source", "billing_basis", "has_utility_bill",
            "utility", "arrays", "to", "cc", "subject", "attachments", "delivery",
            "payment_summary", "paid_usd", "outstanding_usd", "refunded_usd", "warnings",
            "send_mode", "delivery_mode", "cadence")
    return {k: x.get(k) for k in keep}


def _slim_outgoing(x: dict) -> dict:
    keep = ("kind", "draft_id", "invoice_id", "subscription_id", "customer_name", "email",
            "send_mode", "delivery_mode", "cadence", "period_label", "amount_usd",
            "amount_is_estimate", "kwh", "invoice_number", "utility", "arrays", "when",
            "when_label", "status", "reason", "created_at")
    return {k: x.get(k) for k in keep}


def gather(db, tenant, *, include_reconcile=True) -> dict:
    """Everything the auditor looks at, from the same readers the mail room uses."""
    from . import mailroom
    ctx = mailroom.sub_context(db, tenant.id)
    sent, total = mailroom.sent_items(db, tenant.id, limit=1000, offset=0, ctx=ctx)
    known = mailroom.known_frozen_periods(db, tenant.id)
    legacy = mailroom.legacy_items(db, tenant.id, ctx, known)
    outgoing = mailroom.outgoing_items(db, tenant.id, tenant, ctx=ctx)

    subs = []
    for sid, c in ctx.items():
        subs.append({k: v for k, v in c.items() if not k.startswith("_")})

    reconcile = None
    try:
        from .reconcile_bills import reconcile_tenant
        rec = reconcile_tenant(db, tenant.id) if include_reconcile else {}
        reconcile = {
            "status_counts": rec.get("status_counts"),
            "allocation_counts": rec.get("allocation_counts"),
            "allocation_at_stake_usd": rec.get("allocation_at_stake_usd"),
            "subscriptions": [
                {"subscription_id": r.get("subscription_id") or r.get("sub_id"),
                 "customer_name": r.get("customer_name"),
                 "overall_status": r.get("overall_status"),
                 "allocation": (r.get("allocation") or {}).get("status"),
                 "arrays": [{k: a.get(k) for k in ("array_name", "our_kwh", "gmp_kwh",
                                                     "delta_pct", "status", "mismatch_reason")}
                            for a in (r.get("arrays") or [])]}
                for r in (rec.get("subscriptions") or [])
                if (r.get("overall_status") not in (None, "match"))
                or ((r.get("allocation") or {}).get("status") not in (None, "match", "not_gmp"))
            ],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("mailroom audit: reconcile unavailable: %s", e)
        reconcile = {"error": str(e)[:200]}

    tenant_view = {
        "id": tenant.id,
        "company_name": getattr(tenant, "company_name", None) or getattr(tenant, "name", None),
        "product": getattr(tenant, "product", None),
        "sending_paused": bool(getattr(tenant, "sending_paused", False)),
        "default_billing_rate_per_kwh": getattr(tenant, "default_billing_rate_per_kwh", None),
        "default_net_rate_per_kwh": getattr(tenant, "default_net_rate_per_kwh", None),
        "default_net_rate_adder_per_kwh": getattr(tenant, "default_net_rate_adder_per_kwh", None),
        "default_net_rate_adder_until": _iso(getattr(tenant, "default_net_rate_adder_until", None)),
        "default_discount_pct": getattr(tenant, "default_discount_pct", None),
        "offtaker_payment_policy": getattr(tenant, "offtaker_payment_policy", None),
        "stripe_connect_ready": bool(getattr(tenant, "stripe_connect_account_id", None)
                                     and getattr(tenant, "stripe_connect_charges_enabled", False)),
    }
    return {
        "generated_at": _iso(datetime.utcnow()),
        "tenant": tenant_view,
        "subscriptions": subs,
        "outgoing": outgoing,
        "sent": sent + legacy,
        "sent_frozen_total": int(total),
        "coverage": {"checked": len(sent) + len(legacy), "total": int(total) + len(legacy),
                     "truncated": len(sent) < total, "reconcile_checked": include_reconcile},
        "reconcile": reconcile,
    }


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _dt(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


# ── deterministic checks ───────────────────────────────────────────────────

# The suggested fix every rule carries when the call site does not spell one
# out, and the place in the app that fixes it (the UI renders one button per
# target: open the invoice drawer, open the off-taker card, open the draft,
# the Bill audit tab, the delivery-holds panel, payment collection, the cycle
# card's resume switch).
_DEFAULT_FIX = {
    "duplicate_period": "Refund or credit the second invoice, then check the exactly-once guard on that off-taker before the next run.",
    "amount_jump": "Open the invoice and compare its kWh and rate to the utility bill for the same period; if the bill is right, the jump is real — say so in the email note.",
    "rate_not_entered": "Enter the contract rate (and any incentive adder) on the off-taker so invoices stop pricing from the bill's credit line.",
    "no_utility_bill": "Bind the off-taker to its utility account so the invoice is computed from the settled bill, then re-issue.",
    "rate_outlier": "Check the rate on the off-taker; a decimal-point slip is the usual cause.",
    "zero_amount": "Confirm the bill really shows no excess for the period, or that a credit legitimately zeroed it; otherwise fix the share or the binding.",
    "zero_kwh": "Confirm the utility bill for the period shows generation; if it does, the binding or the share is wrong.",
    "bounced": "Correct the email address on the off-taker and re-send the invoice.",
    "unconfirmed_send": "Verify the receipt in the delivery holds panel with the provider's email id; do not re-send until it is settled.",
    "no_recipient": "Add the off-taker's billing email and set the recipient slider to the customer.",
    "unpaid_aging": "Send a reminder; record the payment as an offline receipt if a check arrived.",
    "payment_not_tracked": "Record the check when it arrives, or finish Stripe Connect so a pay link mints on the next invoice.",
    "stale_draft": "Open the draft and approve, edit or dismiss it.",
    "held_invoice": "Read the hold reason; most holds clear when the utility bill for the period is captured.",
    "retrying_invoice": "Nothing to do until the retry window; if it keeps failing, check the mailer status.",
    "unconfirmed_invoice": "Verify the provider receipt in delivery holds before anything is re-sent.",
    "auto_to_me": "Move the recipient slider to the customer if this off-taker should receive invoices directly.",
    "missing_email": "Add the off-taker's billing email.",
    "over_allocated": "Correct the shares on the listed off-takers so the array totals 100% before the next run.",
    "gmp_mismatch": "Open Bill audit for this off-taker and re-check the utility-account binding and the period.",
    "gmp_share_mismatch": "Compare the entered share to what the utility credited on the bill; fix whichever is wrong.",
    "legacy_history": "No action; newer invoices keep full evidence automatically.",
    "paused": "Resume sending from the cycle card when you are ready.",
}


def _targets(code: str, *, subscription_id=None, invoice_id=None, draft_id=None) -> list[dict]:
    t: list[dict] = []
    if invoice_id is not None and not str(invoice_id).startswith("legacy"):
        t.append({"kind": "invoice", "id": invoice_id, "label": "Open invoice"})
    elif invoice_id is not None:
        t.append({"kind": "invoice", "id": invoice_id, "label": "Open invoice"})
    if draft_id is not None:
        t.append({"kind": "draft", "id": draft_id, "subscription_id": subscription_id, "label": "Open draft"})
    if subscription_id is not None:
        t.append({"kind": "offtaker", "id": subscription_id, "label": "Open off-taker"})
    if code in ("gmp_mismatch", "gmp_share_mismatch", "no_utility_bill"):
        t.append({"kind": "bill_audit", "label": "Bill audit"})
    if code in ("unconfirmed_send", "unconfirmed_invoice", "held_invoice", "retrying_invoice"):
        t.append({"kind": "holds", "label": "Delivery holds"})
    if code in ("unpaid_aging", "payment_not_tracked"):
        t.append({"kind": "collection", "label": "Payment collection"})
    if code == "paused":
        t.append({"kind": "cycle", "label": "Resume sending"})
    return t


def _f(code: str, severity: str, title: str, detail: str, *, subscription_id=None,
       invoice_id=None, customer_name=None, evidence: Optional[dict] = None,
       action: Optional[str] = None, draft_id=None) -> dict:
    fix = action or _DEFAULT_FIX.get(code) or "Open it and check the figures against the utility bill."
    if draft_id is None and isinstance(evidence, dict) and evidence.get("draft_id") is not None:
        draft_id = evidence.get("draft_id")
    return {"code": code, "severity": severity, "title": title, "detail": detail,
            "subscription_id": subscription_id, "invoice_id": invoice_id,
            "customer_name": customer_name, "evidence": evidence or {},
            "action": fix, "fix": fix, "source": "rules",
            "targets": _targets(code, subscription_id=subscription_id,
                                invoice_id=invoice_id, draft_id=draft_id)}


def deterministic_checks(payload: dict, now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.utcnow()
    out: list[dict] = []
    sent = [x for x in payload.get("sent") or [] if not x.get("legacy")]
    legacy = [x for x in payload.get("sent") or [] if x.get("legacy")]
    outgoing = payload.get("outgoing") or []
    subs = payload.get("subscriptions") or []
    by_sub_sent: dict[int, list[dict]] = {}
    for x in sent:
        by_sub_sent.setdefault(x["subscription_id"], []).append(x)

    # 1. Same month billed twice (frozen rows).
    for sid, rows in by_sub_sent.items():
        seen: dict[str, dict] = {}
        for x in rows:
            m = str(x.get("period_end") or x.get("period_key") or "")[:7]
            if not m or x.get("kind") == "trueup" or x.get("status") != "accepted":
                continue
            if m in seen:
                out.append(_f("duplicate_period", "critical", "Same month invoiced twice",
                              f"{x.get('customer_name')} has two accepted invoices for {m}: "
                              f"#{seen[m].get('invoice_number')} and #{x.get('invoice_number')}.",
                              subscription_id=sid, invoice_id=x["id"], customer_name=x.get("customer_name"),
                              evidence={"month": m, "invoice_ids": [seen[m]["id"], x["id"]]},
                              action="Refund or credit the duplicate and check the exactly-once guard."))
            else:
                seen[m] = x

    # 2. Amount jumped vs the previous invoice for the same off-taker.
    for sid, rows in by_sub_sent.items():
        ordered = sorted([r for r in rows if r.get("kind") != "trueup" and r.get("amount_usd") is not None],
                         key=lambda r: r.get("sent_at") or "")
        for prev, cur in zip(ordered, ordered[1:]):
            a, b = float(prev["amount_usd"]), float(cur["amount_usd"])
            if a <= 0:
                continue
            jump = (b - a) / a
            if abs(jump) >= 0.5 and abs(b - a) >= 25:
                out.append(_f("amount_jump", "high", "Invoice amount moved sharply",
                              f"{cur.get('customer_name')}: ${a:,.2f} → ${b:,.2f} ({jump:+.0%}) between "
                              f"{prev.get('period_label')} and {cur.get('period_label')}.",
                              subscription_id=sid, invoice_id=cur["id"], customer_name=cur.get("customer_name"),
                              evidence={"previous_usd": a, "current_usd": b, "change_pct": round(jump * 100, 1),
                                        "previous_kwh": prev.get("kwh"), "current_kwh": cur.get("kwh")},
                              action="Confirm the kWh and rate on the utility bill before the next cycle."))

    # 3-5. Per-invoice figure sanity.
    for x in sent:
        rate = x.get("rate") or {}
        eff = rate.get("effective_rate_per_kwh")
        cn = x.get("customer_name")
        if rate.get("operator_entered") is False:
            out.append(_f("rate_not_entered", "high", "Billed at a rate nobody entered",
                          f"{cn} #{x.get('invoice_number')} was priced from {rate.get('source') or 'an inferred source'} "
                          f"({rate.get('note') or 'no operator-entered rate on file'}).",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                          evidence={"rate": rate}, action="Enter the contract rate on the off-taker."))
        if x.get("has_utility_bill") is False:
            out.append(_f("no_utility_bill", "critical", "Invoice not backed by a utility bill",
                          f"{cn} #{x.get('invoice_number')} was computed from {x.get('kwh_source') or 'telemetry'}, "
                          "not from the utility's settled bill.",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                          evidence={"kwh_source": x.get("kwh_source"), "billing_basis": x.get("billing_basis")},
                          action="Bind the off-taker to its utility account and re-issue from the bill."))
        try:
            if eff is not None and not (0.03 <= float(eff) <= 0.60):
                out.append(_f("rate_outlier", "high", "Effective rate outside the plausible band",
                              f"{cn} #{x.get('invoice_number')} effective rate ${float(eff):.5f}/kWh.",
                              subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                              evidence={"effective_rate_per_kwh": eff}))
        except (TypeError, ValueError):
            pass
        if x.get("amount_usd") is not None and float(x["amount_usd"]) <= 0 and x.get("kind") != "trueup":
            out.append(_f("zero_amount", "medium", "Zero-dollar invoice went out",
                          f"{cn} #{x.get('invoice_number')} for {x.get('period_label')} billed $0.00.",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                          evidence={"kwh": x.get("kwh"), "credit_applied_usd": x.get("credit_applied_usd")}))
        if x.get("kwh") is not None and float(x["kwh"] or 0) <= 0 and x.get("kind") != "trueup":
            out.append(_f("zero_kwh", "medium", "Invoice with zero kWh",
                          f"{cn} #{x.get('invoice_number')} shows no generation for {x.get('period_label')}.",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn))
        d = x.get("delivery") or {}
        if d.get("status") == "bounced":
            out.append(_f("bounced", "high", "Invoice email bounced",
                          f"{cn} #{x.get('invoice_number')} to {', '.join(x.get('to') or [])}: {d.get('reason') or 'bounced'}.",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                          evidence=d))
        elif d.get("status") == "unconfirmed":
            out.append(_f("unconfirmed_send", "high", "Mailer never confirmed this send",
                          f"{cn} #{x.get('invoice_number')}: {d.get('reason') or 'no provider receipt'}.",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                          action="Verify the receipt in the delivery holds panel before re-sending."))
        if not x.get("to"):
            out.append(_f("no_recipient", "high", "Invoice has no customer recipient",
                          f"{cn} #{x.get('invoice_number')} was sent with no To address on record "
                          f"(send mode {x.get('send_mode')}).",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn))
        sent_at = _dt(x.get("sent_at"))
        if (sent_at and x.get("payment_summary") in ("unpaid", "partial")
                and (now - sent_at).days >= 35):
            out.append(_f("unpaid_aging", "medium", "Unpaid for more than 35 days",
                          f"{cn} #{x.get('invoice_number')} ${x.get('outstanding_usd')} outstanding since "
                          f"{sent_at:%b %d}.",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                          evidence={"outstanding_usd": x.get("outstanding_usd"), "days": (now - sent_at).days}))
        if x.get("payment_summary") == "not_tracked" and sent_at and (now - sent_at).days >= 3:
            out.append(_f("payment_not_tracked", "low", "No way to tell if this invoice was paid",
                          f"{cn} #{x.get('invoice_number')} has no pay link and no offline receipt.",
                          subscription_id=x["subscription_id"], invoice_id=x["id"], customer_name=cn,
                          action="Record the check when it arrives, or connect Stripe so the link mints."))

    # 6. Going-out hygiene.
    for o in outgoing:
        cn = o.get("customer_name")
        created = _dt(o.get("created_at"))
        if o.get("kind") == "draft" and created and (now - created).days >= 10:
            out.append(_f("stale_draft", "medium", "Draft waiting more than 10 days",
                          f"{cn}: {o.get('period_label')} for ${o.get('amount_usd')} has sat since {created:%b %d}.",
                          subscription_id=o.get("subscription_id"), customer_name=cn,
                          evidence={"draft_id": o.get("draft_id")}, action="Approve, edit or dismiss it."))
        if o.get("kind") in ("held", "retrying", "unconfirmed"):
            sev = "high" if o.get("kind") == "unconfirmed" else "medium"
            out.append(_f(f"{o['kind']}_invoice", sev, {"held": "Invoice is held", "retrying": "Invoice send is retrying",
                                                          "unconfirmed": "Invoice send unconfirmed"}[o["kind"]],
                          f"{cn} {o.get('period_label')}: {o.get('reason') or o.get('when_label')}.",
                          subscription_id=o.get("subscription_id"), invoice_id=o.get("invoice_id"),
                          customer_name=cn, evidence={"reason": o.get("reason"), "when": o.get("when")}))
        if o.get("kind") == "scheduled" and o.get("status") == "auto" and (o.get("send_mode") or "to_me") == "to_me":
            out.append(_f("auto_to_me", "low", "Auto-send goes to you, not the customer",
                          f"{cn} is set to auto-send with the recipient slider on 'to me'.",
                          subscription_id=o.get("subscription_id"), customer_name=cn))
        if (o.get("send_mode") in ("to_client", "to_both")) and not o.get("email"):
            out.append(_f("missing_email", "high", "Customer recipient missing",
                          f"{cn} is set to send to the customer but has no email on file.",
                          subscription_id=o.get("subscription_id"), customer_name=cn,
                          action="Add the off-taker's email."))

    # 7. Allocation shares per array across enabled subscriptions.
    per_array: dict[str, float] = {}
    who: dict[str, list[str]] = {}
    for s in subs:
        if not s.get("enabled"):
            continue
        row = s.get("_row")
        pct = s.get("allocation_pct")
        for name in s.get("arrays") or []:
            try:
                per_array[name] = per_array.get(name, 0.0) + float(pct or 0)
            except (TypeError, ValueError):
                pass
            who.setdefault(name, []).append(s.get("customer_name") or str(s.get("subscription_id")))
    for name, total in per_array.items():
        if total > 1.0005:
            out.append(_f("over_allocated", "critical", "Array allocated over 100%",
                          f"{name}: enabled off-takers sum to {total * 100:.2f}% ({len(who[name])} off-takers).",
                          evidence={"array": name, "total_pct": round(total * 100, 2), "offtakers": who[name][:20]},
                          action="Fix the shares before the next run; every invoice on this array is overstated."))

    # 8. GMP cross-check mismatches.
    rec = payload.get("reconcile") or {}
    for r in rec.get("subscriptions") or []:
        if r.get("overall_status") == "mismatch":
            arrs = [a for a in (r.get("arrays") or []) if a.get("status") == "mismatch"]
            out.append(_f("gmp_mismatch", "high", "Invoice kWh does not match the GMP bill",
                          f"{r.get('customer_name')}: " + "; ".join(
                              f"{a.get('array_name')}: ours {a.get('our_kwh')} vs GMP {a.get('gmp_kwh')} ({a.get('delta_pct')}%)"
                              for a in arrs[:3]),
                          subscription_id=r.get("subscription_id"), customer_name=r.get("customer_name"),
                          evidence={"arrays": arrs[:5]}, action="Open the Bill audit and re-check the binding."))
        if r.get("allocation") in ("mismatch", "flagged"):
            out.append(_f("gmp_share_mismatch", "high", "GMP's credited share disagrees with the entered share",
                          f"{r.get('customer_name')}: GMP's credit implies a different share than the one billed.",
                          subscription_id=r.get("subscription_id"), customer_name=r.get("customer_name")))

    # 9. Legacy history without evidence.
    if legacy:
        out.append(_f("legacy_history", "info", "Older invoices lack frozen evidence",
                      f"{len(legacy)} invoice(s) were issued before frozen evidence existed; amounts are from the "
                      "send stamps, and their emails and attachments cannot be reproduced exactly.",
                      evidence={"count": len(legacy)}))
    if payload.get("tenant", {}).get("sending_paused"):
        out.append(_f("paused", "info", "Sending is paused",
                      "The scheduler will not send or draft anything until sending is resumed."))
    return out


# ── model review ───────────────────────────────────────────────────────────

FINDINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["ready", "caution", "stop"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "subscription_id": {"type": ["integer", "null"]},
                    "invoice_id": {"type": ["integer", "null"]},
                    "customer_name": {"type": ["string", "null"]},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "title", "detail", "subscription_id", "invoice_id",
                             "customer_name", "fix"],
            },
        },
        "what_to_look_for": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary", "findings", "what_to_look_for"],
}

SYSTEM_PROMPT = """You are an independent billing auditor for a community-solar operator.
You are given the operator's mail room: every off-taker invoice that was issued (with the
frozen figures that were billed, the recipients, delivery and payment state), everything
queued to go out, each off-taker's configuration, the utility cross-check, and a list of
findings a rule engine already produced.

Your job: find what the rules could not. Think like a careful billing clerk about to let
these invoices reach real customers. Look for: inconsistent pricing across similar
off-takers, shares that look wrong for the array, kWh that do not fit the season or the
neighbours, recipients that look like the wrong person, periods that skip or overlap,
credits that look mis-applied, patterns in holds or bounces, and anything you would
want a human to double-check before money moves. Do not repeat a rule finding unless you
add something. Every finding must cite the subscription_id and, when it concerns a
specific invoice, the invoice_id from the data. Be concrete: say the numbers. If the
evidence is clean, say so plainly and give a 'ready' verdict — do not invent problems.
Use 'stop' only when a real customer would receive a wrong invoice or a wrong amount
would be collected. Keep the summary to three sentences.

For every finding give a concrete "fix": the single next action the operator should
take, in one sentence, naming the off-taker or invoice. Also return "what_to_look_for":
three to eight short checks the operator should do by eye before the next invoices go
out — specific to THIS board (name the off-takers, periods, amounts or rates worth a
second look), not generic advice. If the board is empty, say what to check once the
first invoices exist."""


def _bounded_json(payload: dict) -> str:
    sent = [_slim_sent(x) for x in (payload.get("sent") or [])[:MAX_SENT_FOR_MODEL]]
    slim = {
        "tenant": payload.get("tenant"),
        "subscriptions": payload.get("subscriptions"),
        "outgoing": [_slim_outgoing(x) for x in payload.get("outgoing") or []],
        "sent": sent,
        "sent_total": payload.get("sent_frozen_total"),
        "reconcile": dict(payload.get("reconcile") or {}),
    }
    txt = json.dumps(slim, default=str)
    while len(txt) > MAX_PAYLOAD_CHARS and slim["sent"]:
        slim["sent"] = slim["sent"][: max(0, len(slim["sent"]) // 2)]
        slim["truncated"] = True
        txt = json.dumps(slim, default=str)
    # The roster / queue can exceed the budget even after history is empty.
    # Bound every list and explicitly tell the reviewer coverage is partial.
    while len(txt) > MAX_PAYLOAD_CHARS:
        candidates = [(slim, key) for key in ("subscriptions", "outgoing")
                      if isinstance(slim.get(key), list) and slim[key]]
        rec = slim.get("reconcile")
        if isinstance(rec, dict) and isinstance(rec.get("subscriptions"), list) and rec["subscriptions"]:
            candidates.append((rec, "subscriptions"))
        if not candidates:
            return json.dumps({"truncated": True, "sent_total": slim.get("sent_total"),
                "note": "Evidence exceeded the model budget; deterministic findings remain authoritative."})
        owner, key = max(candidates, key=lambda item: len(json.dumps(item[0][item[1]], default=str)))
        owner[key] = owner[key][:len(owner[key]) // 2]
        slim["truncated"] = True
        txt = json.dumps(slim, default=str)
    return txt


CLI_ADDENDUM = """

=== HOW TO ANSWER HERE ===
Put the complete audit JSON object — {"verdict", "summary", "findings",
"what_to_look_for"} exactly as described above — as the value of "content" (a JSON string), and leave
"tool_calls" empty. Nothing else."""


def _friendly_api_error(e: Exception) -> str:
    """The reason a layer was skipped, in the operator's words."""
    txt = ""
    resp = getattr(e, "response", None)
    try:
        txt = (resp.text if resp is not None else "") or str(e)
    except Exception:  # noqa: BLE001
        txt = str(e)
    low = txt.lower()
    if "credit balance" in low:
        return ("the Anthropic API key on the server has no credit left (top up under "
                "Plans & Billing at console.anthropic.com)")
    if "api key" in low and ("invalid" in low or "authentication" in low):
        return "the Anthropic API key on the server was rejected"
    if "rate limit" in low or "429" in low:
        return "the Anthropic API rate limit was hit — try again in a minute"
    if "busy" in low:
        return "the Claude CLI is busy with another task — try again in a minute"
    if "not enabled" in low:
        return "the Claude CLI is not enabled on the server"
    return str(e)[:200]


def _review_via_api(system: str, user: str, model: Optional[str]) -> dict:
    from .repro import llm
    if not llm.llm_available():
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    obj = llm.call_json(system=system, user_text=user, schema=FINDINGS_SCHEMA,
                        max_tokens=4096, model=model)
    return {"obj": obj, "model": model or llm.REPRO_LLM_MODEL, "provider": "anthropic"}


def _review_via_cli(system: str, user: str) -> dict:
    """Ford's Claude subscription through the Claude Code CLI — the same brain
    the Energy Agent runs on (EA_CLAUDE_CLI=1). One call, no tools."""
    from .. import claude_cli
    if not claude_cli.enabled():
        raise RuntimeError("claude-cli not enabled")
    res = claude_cli.call(
        [{"role": "system", "content": system + CLI_ADDENDUM},
         {"role": "user", "content": user}],
        [], max_tokens=4096)
    msg = res.get("message") or {}
    content = msg.get("content")
    obj = content if isinstance(content, dict) else claude_cli._extract_json(str(content or ""))
    if not isinstance(obj, dict) or "findings" not in obj:
        raise RuntimeError("claude-cli reply was not the audit JSON")
    return {"obj": obj, "model": ",".join(claude_cli._models()), "provider": "claude-cli"}


def _review_order() -> list[str]:
    primary = (os.getenv("ENERGY_AGENT_LLM_PRIMARY") or "").strip().lower()
    if primary in ("claude-cli", "claude_cli", "cli", "subscription", "max"):
        return ["cli", "api"]
    return ["api", "cli"]


def model_review(payload: dict, rule_findings: list[dict]) -> dict:
    """Ask Claude — on the subscription (CLI) or the metered API, in the order
    the Energy Agent uses. Returns {ok, verdict, summary, findings, model,
    provider, seconds} or {ok: False, error} — never raises."""
    model = os.getenv("MAILROOM_AUDIT_MODEL") or None
    user = (
        "MAIL ROOM DATA (JSON):\n" + _bounded_json(payload) +
        "\n\nRULE FINDINGS ALREADY RAISED (JSON):\n" +
        json.dumps([{k: f.get(k) for k in ("code", "severity", "title", "subscription_id", "invoice_id")}
                    for f in rule_findings], default=str)
    )
    t0 = time.time()
    errors: list[str] = []
    got = None
    for how in _review_order():
        try:
            got = _review_via_cli(SYSTEM_PROMPT, user) if how == "cli" else \
                  _review_via_api(SYSTEM_PROMPT, user, model)
            break
        except Exception as e:  # noqa: BLE001
            reason = _friendly_api_error(e)
            logger.warning("mailroom audit: %s review failed: %s", how, reason)
            errors.append(f"{'Claude CLI' if how == 'cli' else 'Anthropic API'}: {reason}")
    if got is None:
        return {"ok": False, "error": "; ".join(errors) or "no model available"}
    res = got["obj"]
    findings = []
    for f in (res.get("findings") or []) if isinstance(res, dict) else []:
        if not isinstance(f, dict):
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "medium"
        fix = str(f.get("fix") or f.get("action") or "").strip()[:600] or None
        findings.append({"code": "model", "severity": sev, "title": str(f.get("title") or "")[:200],
                         "detail": str(f.get("detail") or "")[:2000],
                         "subscription_id": f.get("subscription_id"), "invoice_id": f.get("invoice_id"),
                         "customer_name": f.get("customer_name"), "action": fix, "fix": fix,
                         "evidence": {}, "source": "model",
                         "targets": _targets("model", subscription_id=f.get("subscription_id"),
                                             invoice_id=f.get("invoice_id"))})
    verdict = res.get("verdict") if isinstance(res, dict) else None
    look = [str(x).strip()[:300] for x in ((res.get("what_to_look_for") or []) if isinstance(res, dict) else [])
            if str(x).strip()][:8]
    return {"ok": True, "verdict": verdict if verdict in ("ready", "caution", "stop") else None,
            "summary": (res.get("summary") if isinstance(res, dict) else None),
            "findings": findings, "what_to_look_for": look,
            "model": got["model"], "provider": got["provider"],
            "seconds": round(time.time() - t0, 1), "skipped": errors or None}


# ── what to look for ───────────────────────────────────────────────────────

def look_for(payload: dict, findings: list[dict]) -> list[str]:
    """A short checklist for the operator's own eyes, specific to this board.
    The model may add to it; these are the checks the rules cannot make for
    them (recipients that look wrong, amounts that only a human would
    question, the first-run essentials on an empty book)."""
    sent = [x for x in payload.get("sent") or [] if not x.get("legacy")]
    legacy = [x for x in payload.get("sent") or [] if x.get("legacy")]
    outgoing = payload.get("outgoing") or []
    subs = [x for x in payload.get("subscriptions") or [] if x.get("enabled")]
    codes = {f.get("code") for f in findings}
    out: list[str] = []

    if not subs and not sent and not legacy:
        out += [
            "Load the off-taker roster and bind every off-taker to its utility account before the first run.",
            "Enter each off-taker's contract rate (or the master rate) so nothing prices from the bill's credit line.",
            "Send one test invoice to yourself and open it here: check the letterhead, the recipient, the period and the figures.",
            "Set the recipient slider to the customer only for off-takers whose email you have confirmed.",
        ]
        return out[:10]

    drafts = [o for o in outgoing if o.get("kind") == "draft"]
    if drafts:
        names = ", ".join(sorted({o.get("customer_name") or "?" for o in drafts})[:4])
        out.append(f"Open the {len(drafts)} draft{'s' if len(drafts) != 1 else ''} waiting on you ({names}{'…' if len(drafts) > 4 else ''}) and read the amount and period before approving.")
    held = [o for o in outgoing if o.get("kind") in ("held", "retrying", "unconfirmed")]
    if held:
        out.append(f"Read the reason on the {len(held)} held or unconfirmed invoice{'s' if len(held) != 1 else ''} — a missing utility bill is the usual cause, a mailer refusal is not.")
    to_me = [o for o in outgoing if (o.get("send_mode") or "to_me") == "to_me"]
    if to_me and len(to_me) == len(outgoing) and outgoing:
        out.append("Every queued invoice is still addressed to you (recipient slider on 'to me'); move the slider for off-takers who should receive theirs directly.")
    if sent:
        newest = sent[:3]
        names = ", ".join(f"{x.get('customer_name')} ({x.get('period_label') or x.get('period_key')})" for x in newest)
        out.append(f"Open the newest invoices — {names} — and compare the kWh on each to the utility bill for that period.")
        inferred = [x for x in sent if (x.get("rate") or {}).get("operator_entered") is False]
        if inferred and "rate_not_entered" not in codes:
            out.append(f"{len(inferred)} invoice{'s' if len(inferred) != 1 else ''} priced from an inferred rate: confirm the contract rate on those off-takers.")
        recips = {}
        for x in sent:
            for a in x.get("to") or []:
                recips.setdefault(a.lower(), set()).add(x.get("customer_name"))
        shared = {a: n for a, n in recips.items() if len(n) > 1}
        if shared:
            a, n = next(iter(shared.items()))
            out.append(f"{a} receives invoices for {len(n)} different off-takers ({', '.join(sorted(x for x in n if x)[:3])}) — make sure that is intended.")
        unpaid = [x for x in sent if x.get("payment_summary") in ("unpaid", "partial")]
        if unpaid:
            due = sum(float(x.get("outstanding_usd") or 0) for x in unpaid)
            out.append(f"{len(unpaid)} invoice{'s' if len(unpaid) != 1 else ''} still open, ${due:,.2f} outstanding — decide who gets a reminder.")
        by_sub: dict = {}
        for x in sent:
            by_sub.setdefault(x.get("subscription_id"), []).append(x)
        gaps = [c for c, rows in by_sub.items() if len(rows) >= 2]
        if gaps and "amount_jump" not in codes:
            out.append("Scan each off-taker's amounts month to month; anything that moved more than a quarter deserves a reason you can name.")
    if legacy and not sent:
        out.append(f"The {len(legacy)} older invoice{'s' if len(legacy) != 1 else ''} here have no frozen email or attachment; the first new run will be the first you can audit line by line.")
    if subs:
        no_email = [x for x in subs if not x.get("client_email")]
        if no_email and "missing_email" not in codes:
            out.append(f"{len(no_email)} enabled off-taker{'s have' if len(no_email) != 1 else ' has'} no email on file.")
        out.append("Check that each array's shares add up to 100% and that no off-taker appears twice under slightly different names.")
    return out[:10]


# ── the run ────────────────────────────────────────────────────────────────

_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def _verdict_from(findings: list[dict], model_verdict: Optional[str]) -> str:
    worst = min((_SEV_RANK.get(f.get("severity"), 99) for f in findings), default=99)
    rules = "stop" if worst == 0 else ("caution" if worst <= 2 else "ready")
    order = {"ready": 0, "caution": 1, "stop": 2}
    if model_verdict in order and order[model_verdict] > order[rules]:
        return model_verdict
    return rules


def execute_run(run_id: int, *, tenant_id: str, rules_only=False) -> None:
    """Runs in its own thread with its own session (pool-leak rule)."""
    from ..db import SessionLocal
    from ..models import OfftakerAuditRun, Tenant
    t0 = time.time()
    try:
        repair_accepted_dispatches(run_id, tenant_id)
        with SessionLocal() as db:
            tenant = db.get(Tenant, tenant_id)
            payload = gather(db, tenant, include_reconcile=not rules_only)
        rules = deterministic_checks(payload)
        with SessionLocal() as db:
            current_run = db.get(OfftakerAuditRun, run_id)
            for repair in ((current_run.stats or {}).get("repairs") or []) if current_run else []:
                if repair.get("status") in ("blocked", "failed"):
                    rules.append(_f("repair_requires_review", "high", "Send bookkeeping needs review",
                        repair["reason"], invoice_id=repair.get("invoice_id"),
                        action="Review the frozen invoice and accepted dispatch evidence.",
                        evidence={"dispatch_id": repair.get("dispatch_id")}))
        if (payload.get("coverage") or {}).get("truncated"):
            rules.append(_f("partial_coverage", "medium", "Older invoice history was not checked",
                "This bounded check reviewed the newest 1,000 frozen invoices. Older history remains available.",
                evidence=payload["coverage"], action="Review older invoice history separately."))
        review = ({"ok": False, "skipped": "Rules-only check; no model requested"}
                  if rules_only else model_review(payload, rules))
        model_findings = []
        for f in (review.get("findings") or []):
            if isinstance(f, dict):
                f = dict(f)
                f.setdefault("source", "model")
                f.setdefault("code", "model")
                f.setdefault("evidence", {})
                if not f.get("fix") and f.get("action"):
                    f["fix"] = f["action"]
                if not f.get("targets"):
                    f["targets"] = _targets("model", subscription_id=f.get("subscription_id"),
                                            invoice_id=f.get("invoice_id"))
                model_findings.append(f)
        findings = sorted(rules + model_findings,
                          key=lambda f: (_SEV_RANK.get(f.get("severity"), 99), f.get("customer_name") or ""))
        verdict = _verdict_from(findings, review.get("verdict") if review.get("ok") else None)
        seen_lf: set[str] = set()
        what_to_look_for: list[str] = []
        for item in (review.get("what_to_look_for") or []) + look_for(payload, findings):
            k = item.strip().lower()[:80]
            if k and k not in seen_lf:
                seen_lf.add(k)
                what_to_look_for.append(item)
        what_to_look_for = what_to_look_for[:10]
        stats = {
            "what_to_look_for": what_to_look_for,
            "check_mode": "check" if rules_only else "deep",
            "coverage": payload.get("coverage") or {},
            "subscriptions": len(payload.get("subscriptions") or []),
            "outgoing": len(payload.get("outgoing") or []),
            "sent": len(payload.get("sent") or []),
            "sent_frozen": payload.get("sent_frozen_total"),
            "remaining_findings": len(findings),
            "rule_findings": len(rules),
            "model_findings": len(review.get("findings") or []),
            "by_severity": {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES},
            "model_ok": bool(review.get("ok")),
            "model_error": review.get("error"),
            "model_skipped": review.get("skipped"),
            "model_seconds": review.get("seconds"),
            "seconds": round(time.time() - t0, 1),
        }
        with SessionLocal() as db:
            from sqlalchemy import text
            if db.bind.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            run = db.scalar(select(OfftakerAuditRun).where(OfftakerAuditRun.id == run_id).with_for_update())
            if run is None or run.tenant_id != tenant_id or run.status != "running":
                return
            repairs = list((run.stats or {}).get("repairs") or [])
            stats["repairs"] = repairs
            stats["repaired_count"] = sum(r.get("status") == "repaired" for r in repairs)
            run.status = "done"
            run.finished_at = datetime.utcnow()
            run.model = review.get("model") if review.get("ok") else None
            run.provider = review.get("provider") if review.get("ok") else "rules-only"
            run.verdict = verdict
            run.summary = (review.get("summary") if review.get("ok") else
                           _rules_summary(findings, review.get("error")))
            run.findings = findings
            run.stats = stats
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("mailroom audit run %s failed", run_id)
        try:
            with SessionLocal() as db:
                from sqlalchemy import text
                if db.bind.dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                run = db.scalar(select(OfftakerAuditRun).where(OfftakerAuditRun.id == run_id).with_for_update())
                if run is not None and run.tenant_id == tenant_id and run.status == "running":
                    run.status = "failed"
                    run.finished_at = datetime.utcnow()
                    run.error = str(e)[:2000]
                    db.commit()
        except Exception:  # noqa: BLE001
            pass


def _rules_summary(findings: list[dict], model_error: Optional[str]) -> str:
    n = len(findings)
    worst = next((f for f in findings), None)
    head = ("No rule violations found." if n == 0 else
            f"{n} rule finding(s); most severe: {worst.get('severity')} — {worst.get('title')}.")
    tail = f" Model review unavailable: {model_error}." if model_error else ""
    return head + tail


def repair_accepted_dispatches(run_id, tenant_id):
    """Bounded deterministic repair; never call a mailer, model or payment API."""
    from sqlalchemy import String, cast, or_
    from ..db import SessionLocal
    from ..models import BillingEmailDispatch, OfftakerInvoice
    from .issuance import repair_accepted_invoice
    with SessionLocal() as db:
        ids = db.scalars(select(OfftakerInvoice.id).join(BillingEmailDispatch,
            (BillingEmailDispatch.tenant_id == OfftakerInvoice.tenant_id)
            & (BillingEmailDispatch.key == "invoice:" + cast(OfftakerInvoice.id, String))
        ).where(OfftakerInvoice.tenant_id == tenant_id,
                BillingEmailDispatch.status == "accepted",
                or_(OfftakerInvoice.status != "accepted", OfftakerInvoice.applied_at.is_(None)))
            .order_by(OfftakerInvoice.period_end, OfftakerInvoice.id).limit(1000)).all()
    for invoice_id in ids:
        try:
            repair_accepted_invoice(tenant_id=tenant_id, invoice_id=invoice_id, audit_run_id=run_id)
        except Exception as exc:
            logger.warning("Mail Room repair failed for invoice %s: %s", invoice_id, type(exc).__name__)
            from sqlalchemy import text
            from ..models import OfftakerAuditRun
            with SessionLocal() as db:
                if db.bind.dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                run = db.scalar(select(OfftakerAuditRun).where(
                    OfftakerAuditRun.id == run_id, OfftakerAuditRun.tenant_id == tenant_id,
                    OfftakerAuditRun.status == "running").with_for_update())
                if run is not None:
                    stats = dict(run.stats or {})
                    stats["repairs"] = list(stats.get("repairs") or []) + [{
                        "code": "accepted_dispatch_recovered", "invoice_id": invoice_id,
                        "dispatch_id": None, "status": "failed",
                        "reason": "Bookkeeping repair failed; invoice unchanged. Review manually.",
                        "before": None, "after": None}]
                    run.stats = stats
                    db.commit()


def start_run(db, tenant_id: str, *, triggered_by: str = "operator", rules_only=False,
              force=False) -> dict:
    """Tenant-row locking serializes claims across web processes."""
    from sqlalchemy import text
    from ..models import OfftakerAuditRun, Tenant
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    if db.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()) is None:
        raise LookupError("Tenant not found")
    now = datetime.utcnow()
    active_rows = db.scalars(select(OfftakerAuditRun).where(
        OfftakerAuditRun.tenant_id == tenant_id, OfftakerAuditRun.status == "running")
        .order_by(OfftakerAuditRun.id.desc())).all()
    for active in active_rows:
        if active.started_at and now - active.started_at < timedelta(minutes=15):
            db.commit()
            return {"ok": True, "run_id": active.id, "already_running": True,
                    "cached": False, "mode": (active.stats or {}).get("check_mode", "deep")}
        active.status = "failed"
        active.error = "Check interrupted or exceeded 15 minutes; start a fresh check."
        active.finished_at = now
    if rules_only and not force:
        recent = db.scalar(select(OfftakerAuditRun).where(
            OfftakerAuditRun.tenant_id == tenant_id, OfftakerAuditRun.status == "done",
            OfftakerAuditRun.triggered_by == "mailroom_check",
            OfftakerAuditRun.finished_at >= now - timedelta(minutes=15))
            .order_by(OfftakerAuditRun.id.desc()).limit(1))
        if recent is not None:
            db.commit()
            return {"ok": True, "run_id": recent.id, "already_running": False,
                    "cached": True, "mode": "check"}
    run = OfftakerAuditRun(tenant_id=tenant_id, status="running",
        triggered_by="mailroom_check" if rules_only else triggered_by,
        stats={"check_mode": "check" if rules_only else "deep", "repairs": [], "repaired_count": 0})
    db.add(run)
    db.commit()
    rid = run.id
    threading.Thread(target=execute_run, args=(rid,),
        kwargs={"tenant_id": tenant_id, "rules_only": rules_only},
        daemon=True, name=f"mailroom-audit-{rid}").start()
    return {"ok": True, "run_id": rid, "already_running": False, "cached": False,
            "mode": "check" if rules_only else "deep"}


def run_json(run) -> dict:
    return {
        "id": run.id, "status": run.status, "triggered_by": run.triggered_by,
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "model": run.model, "provider": run.provider, "verdict": run.verdict,
        "summary": run.summary, "findings": run.findings or [], "stats": run.stats or {},
        "what_to_look_for": (run.stats or {}).get("what_to_look_for") or [],
        "repairs": (run.stats or {}).get("repairs") or [],
        "repaired_count": (run.stats or {}).get("repaired_count") or 0,
        "coverage": (run.stats or {}).get("coverage") or {},
        "error": run.error,
    }
