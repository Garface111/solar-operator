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

Nothing here mutates an invoice. Findings are advisory and point at the
subscription / invoice they concern so the operator can open it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
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


def gather(db, tenant) -> dict:
    """Everything the auditor looks at, from the same readers the mail room uses."""
    from . import mailroom
    ctx = mailroom.sub_context(db, tenant.id)
    sent, total = mailroom.sent_items(db, tenant.id, limit=1000, offset=0, ctx=ctx)
    known = {(s["subscription_id"], str(s.get("period_end") or "")[:7]) for s in sent}
    legacy = mailroom.legacy_items(db, tenant.id, ctx, known)
    outgoing = mailroom.outgoing_items(db, tenant.id, tenant, ctx=ctx)

    subs = []
    for sid, c in ctx.items():
        subs.append({k: v for k, v in c.items() if not k.startswith("_")})

    reconcile = None
    try:
        from .reconcile_bills import reconcile_tenant
        rec = reconcile_tenant(db, tenant.id)
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
        return datetime.fromisoformat(str(v).replace("Z", ""))
    except ValueError:
        return None


# ── deterministic checks ───────────────────────────────────────────────────

def _f(code: str, severity: str, title: str, detail: str, *, subscription_id=None,
       invoice_id=None, customer_name=None, evidence: Optional[dict] = None,
       action: Optional[str] = None) -> dict:
    return {"code": code, "severity": severity, "title": title, "detail": detail,
            "subscription_id": subscription_id, "invoice_id": invoice_id,
            "customer_name": customer_name, "evidence": evidence or {},
            "action": action, "source": "rules"}


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
            if not m or x.get("kind") == "trueup":
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
                          evidence=d, action="Fix the address and re-send."))
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
                    "action": {"type": ["string", "null"]},
                },
                "required": ["severity", "title", "detail", "subscription_id", "invoice_id",
                             "customer_name", "action"],
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
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
would be collected. Keep the summary to three sentences."""


def _bounded_json(payload: dict) -> str:
    sent = [_slim_sent(x) for x in (payload.get("sent") or [])[:MAX_SENT_FOR_MODEL]]
    slim = {
        "tenant": payload.get("tenant"),
        "subscriptions": payload.get("subscriptions"),
        "outgoing": [_slim_outgoing(x) for x in payload.get("outgoing") or []],
        "sent": sent,
        "sent_total": payload.get("sent_frozen_total"),
        "reconcile": payload.get("reconcile"),
    }
    txt = json.dumps(slim, default=str)
    while len(txt) > MAX_PAYLOAD_CHARS and slim["sent"]:
        slim["sent"] = slim["sent"][: max(0, len(slim["sent"]) // 2)]
        slim["truncated"] = True
        txt = json.dumps(slim, default=str)
    return txt


def model_review(payload: dict, rule_findings: list[dict]) -> dict:
    """Ask Claude. Returns {ok, verdict, summary, findings, model, provider} or
    {ok: False, error} — never raises."""
    from .repro import llm
    model = os.getenv("MAILROOM_AUDIT_MODEL") or None
    if not llm.llm_available():
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set — model review skipped"}
    user = (
        "MAIL ROOM DATA (JSON):\n" + _bounded_json(payload) +
        "\n\nRULE FINDINGS ALREADY RAISED (JSON):\n" +
        json.dumps([{k: f.get(k) for k in ("code", "severity", "title", "subscription_id", "invoice_id")}
                    for f in rule_findings], default=str)
    )
    t0 = time.time()
    try:
        res = llm.call_json(system=SYSTEM_PROMPT, user_text=user, schema=FINDINGS_SCHEMA,
                            max_tokens=4096, model=model)
    except Exception as e:  # noqa: BLE001
        logger.warning("mailroom audit: model review failed: %s", e)
        return {"ok": False, "error": str(e)[:300]}
    findings = []
    for f in (res.get("findings") or []) if isinstance(res, dict) else []:
        if not isinstance(f, dict):
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "medium"
        findings.append({"code": "model", "severity": sev, "title": str(f.get("title") or "")[:200],
                         "detail": str(f.get("detail") or "")[:2000],
                         "subscription_id": f.get("subscription_id"), "invoice_id": f.get("invoice_id"),
                         "customer_name": f.get("customer_name"), "action": f.get("action"),
                         "evidence": {}, "source": "model"})
    return {"ok": True, "verdict": res.get("verdict") if isinstance(res, dict) else None,
            "summary": (res.get("summary") if isinstance(res, dict) else None),
            "findings": findings, "model": model or llm.REPRO_LLM_MODEL,
            "provider": "anthropic", "seconds": round(time.time() - t0, 1)}


# ── the run ────────────────────────────────────────────────────────────────

_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def _verdict_from(findings: list[dict], model_verdict: Optional[str]) -> str:
    worst = min((_SEV_RANK.get(f.get("severity"), 99) for f in findings), default=99)
    rules = "stop" if worst == 0 else ("caution" if worst <= 2 else "ready")
    order = {"ready": 0, "caution": 1, "stop": 2}
    if model_verdict in order and order[model_verdict] > order[rules]:
        return model_verdict
    return rules


def execute_run(run_id: int, *, tenant_id: str) -> None:
    """Runs in its own thread with its own session (pool-leak rule)."""
    from ..db import SessionLocal
    from ..models import OfftakerAuditRun, Tenant
    t0 = time.time()
    try:
        with SessionLocal() as db:
            tenant = db.get(Tenant, tenant_id)
            payload = gather(db, tenant)
        rules = deterministic_checks(payload)
        review = model_review(payload, rules)
        model_findings = []
        for f in (review.get("findings") or []):
            if isinstance(f, dict):
                f = dict(f)
                f.setdefault("source", "model")
                f.setdefault("code", "model")
                f.setdefault("evidence", {})
                model_findings.append(f)
        findings = sorted(rules + model_findings,
                          key=lambda f: (_SEV_RANK.get(f.get("severity"), 99), f.get("customer_name") or ""))
        verdict = _verdict_from(findings, review.get("verdict") if review.get("ok") else None)
        stats = {
            "subscriptions": len(payload.get("subscriptions") or []),
            "outgoing": len(payload.get("outgoing") or []),
            "sent": len(payload.get("sent") or []),
            "sent_frozen": payload.get("sent_frozen_total"),
            "rule_findings": len(rules),
            "model_findings": len(review.get("findings") or []),
            "by_severity": {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES},
            "model_ok": bool(review.get("ok")),
            "model_error": review.get("error"),
            "model_seconds": review.get("seconds"),
            "seconds": round(time.time() - t0, 1),
        }
        with SessionLocal() as db:
            run = db.get(OfftakerAuditRun, run_id)
            if run is None:
                return
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
                run = db.get(OfftakerAuditRun, run_id)
                if run is not None:
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


def start_run(db, tenant_id: str, *, triggered_by: str = "operator") -> dict:
    """Create the run row and kick off the thread. One running audit per
    tenant at a time; a second click returns the one in flight."""
    from ..models import OfftakerAuditRun
    active = db.execute(
        select(OfftakerAuditRun).where(OfftakerAuditRun.tenant_id == tenant_id,
                                       OfftakerAuditRun.status == "running")
        .order_by(OfftakerAuditRun.id.desc())
    ).scalars().first()
    if active is not None:
        # A run older than 15 minutes is a crashed thread, not a live one.
        if active.started_at and (datetime.utcnow() - active.started_at) < timedelta(minutes=15):
            return {"ok": True, "run_id": active.id, "already_running": True}
        active.status = "failed"
        active.error = "run did not finish (worker restarted?)"
        active.finished_at = datetime.utcnow()
        db.commit()
    run = OfftakerAuditRun(tenant_id=tenant_id, status="running", triggered_by=triggered_by)
    db.add(run)
    db.commit()
    rid = run.id
    threading.Thread(target=execute_run, args=(rid,), kwargs={"tenant_id": tenant_id},
                     daemon=True, name=f"mailroom-audit-{rid}").start()
    return {"ok": True, "run_id": rid, "already_running": False}


def run_json(run) -> dict:
    return {
        "id": run.id, "status": run.status, "triggered_by": run.triggered_by,
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "model": run.model, "provider": run.provider, "verdict": run.verdict,
        "summary": run.summary, "findings": run.findings or [], "stats": run.stats or {},
        "error": run.error,
    }
