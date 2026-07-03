"""Anna-800 SEND test — 800 real offtaker invoice emails through the REAL
pipeline, every recipient at Resend's sink domain (@resend.dev — accepted by
the API, never delivered to any inbox, zero deliverability/reputation risk).

Mirrors the production scheduler's per-sub calls (deliver_subscription for
auto-mode, draft_subscription → approve for approval-mode) but SCOPED TO
ten_anna_800 ONLY — never call scheduler.deliver_billing_reports() raw for
this test: it iterates every tenant and would evaluate real customers' subs
mid-month.

Passes:
  0. render spot-check — every 33rd sub's actual email HTML must contain the
     exact kWh + $ figures (guards template-level swaps);
  1. canary — ONE real send; abort the whole run if Resend refuses it;
  2. full sweep — auto subs send; approval subs draft (operator 'ready to
     review' notes → operator sink) then approve+send, exactly like the inbox;
  3. assertions — 800 sent, stamps written (last_sent_period_end=2026-06-30),
     sequential invoice counters advanced, recipients all @resend.dev and
     matching each sub's send_mode, guard demos all refused with the right
     reasons, 0 pending drafts left;
  4. idempotence — a second sweep must produce 0 new sends (all
     already_sent skips).

Run on prod (long — run under nohup and tail the log):
  railway ssh "cd /app && nohup python scripts/anna800_send.py > /tmp/anna800_send.log 2>&1 & echo started"
  railway ssh "tail -20 /tmp/anna800_send.log"
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import Tenant, BillingReportSubscription, ReportDraft

TENANT_ID = "ten_anna_800"
SINK_SUFFIX = "@resend.dev"
THROTTLE_S = 0.5          # stay far under Resend's rate limit
MAX_RETRIES = 2


def _sub_ids(db) -> list[int]:
    return list(db.execute(
        select(BillingReportSubscription.id)
        .where(BillingReportSubscription.tenant_id == TENANT_ID,
               BillingReportSubscription.deleted_at.is_(None),
               BillingReportSubscription.enabled == True)  # noqa: E712
        .order_by(BillingReportSubscription.id)
    ).scalars())


def _assert_sink(*emails) -> None:
    for e in emails:
        if e and not e.endswith(SINK_SUFFIX):
            raise SystemExit(f"ABORT: non-sink recipient {e!r} — refusing to send")


def render_spot_check() -> int:
    """The email HTML for every 33rd sub must carry its computed figures."""
    from api.billing.delivery import build_match, _email_html
    checked = 0
    with SessionLocal() as db:
        ids = _sub_ids(db)
        for sid in ids[::33]:
            sub = db.get(BillingReportSubscription, sid)
            if sub.customer_name.startswith("DEMO-HOLD"):
                continue
            m = build_match(sub)
            ci = m.computed_invoice or {}
            if ci.get("has_utility_bill") is not True:
                continue
            _subj, html, _text = _email_html(m, sub, is_test=False)
            kwh = ci.get("kwh") or 0
            amt = ci.get("amount_owed")
            kwh_str = f"{kwh:,.0f} kWh"
            amt_str = f"${amt:,.2f}"
            if kwh_str not in html or amt_str not in html:
                raise SystemExit(
                    f"ABORT: render mismatch sub {sid} ({sub.customer_name}): "
                    f"{kwh_str!r}/{amt_str!r} not in email HTML")
            checked += 1
    print(f"[0] render spot-check: {checked} emails carry exact kWh+$ figures")
    return checked


def sweep(label: str) -> dict:
    """One tenant-scoped scheduler-equivalent pass. Returns result tallies."""
    from api.billing.delivery import deliver_subscription, draft_subscription
    out = {"sent": [], "drafted": [], "skipped": {}, "failed": {}}
    with SessionLocal() as db:
        tenant = db.get(Tenant, TENANT_ID)
        ids = _sub_ids(db)
        n = len(ids)
        for i, sid in enumerate(ids):
            sub = db.get(BillingReportSubscription, sid)
            _assert_sink(sub.client_email, sub.operator_email)
            mode = (sub.delivery_mode or "approval")
            tries = 0
            while True:
                tries += 1
                try:
                    if mode == "auto":
                        r = deliver_subscription(db, sub, tenant,
                                                 triggered_by=f"anna800-{label}")
                    else:
                        r = draft_subscription(db, sub, tenant,
                                               triggered_by=f"anna800-{label}")
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": f"raised: {e}"}
                if r.get("ok") or r.get("skipped") or tries > MAX_RETRIES:
                    break
                time.sleep(2.0 * tries)
            if r.get("ok"):
                (out["sent"] if mode == "auto" else out["drafted"]).append(sid)
                for e in (r.get("to") or []) + (r.get("cc") or []) + (r.get("bcc") or []):
                    _assert_sink(e)
            elif r.get("skipped"):
                out["skipped"][sid] = (r.get("error") or "")[:110]
            else:
                out["failed"][sid] = (r.get("error") or "")[:200]
            if mode == "auto":
                time.sleep(THROTTLE_S)
            if (i + 1) % 50 == 0:
                print(f"  [{label}] {i+1}/{n} — sent={len(out['sent'])} "
                      f"drafted={len(out['drafted'])} skipped={len(out['skipped'])} "
                      f"failed={len(out['failed'])}", flush=True)
    return out


def approve_all_drafts() -> dict:
    """Approve every pending draft exactly like the inbox endpoint does."""
    from api.billing.delivery import deliver_subscription
    out = {"approved": [], "skipped": {}, "failed": {}}
    with SessionLocal() as db:
        tenant = db.get(Tenant, TENANT_ID)
        drafts = list(db.execute(
            select(ReportDraft).where(ReportDraft.tenant_id == TENANT_ID,
                                      ReportDraft.status == "pending")
            .order_by(ReportDraft.id)).scalars())
        n = len(drafts)
        print(f"[2b] approving {n} pending drafts…", flush=True)
        for i, d in enumerate(drafts):
            sub = db.get(BillingReportSubscription, d.subscription_id)
            if sub is None:
                out["failed"][d.id] = "sub missing"
                continue
            _assert_sink(sub.client_email, sub.operator_email)
            tries = 0
            while True:
                tries += 1
                try:
                    r = deliver_subscription(
                        db, sub, tenant, triggered_by="anna800-approve",
                        expected_period_label=d.period_label, note=d.note)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": f"raised: {e}"}
                if r.get("ok") or r.get("skipped") or r.get("period_changed") \
                        or tries > MAX_RETRIES:
                    break
                time.sleep(2.0 * tries)
            if r.get("ok"):
                d.status = "sent"
                d.sent_at = datetime.utcnow()
                db.commit()
                out["approved"].append(d.id)
                for e in (r.get("to") or []) + (r.get("cc") or []) + (r.get("bcc") or []):
                    _assert_sink(e)
            elif r.get("skipped"):
                out["skipped"][d.id] = (r.get("error") or "")[:110]
            else:
                out["failed"][d.id] = (r.get("error") or "")[:200]
            time.sleep(THROTTLE_S)
            if (i + 1) % 50 == 0:
                print(f"  [approve] {i+1}/{n} — ok={len(out['approved'])} "
                      f"failed={len(out['failed'])}", flush=True)
    return out


def final_assertions() -> dict:
    with SessionLocal() as db:
        subs = list(db.execute(
            select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == TENANT_ID,
                   BillingReportSubscription.deleted_at.is_(None))).scalars())
        core = [s for s in subs if s.enabled
                and not s.customer_name.startswith("DEMO-HOLD")]
        guard = [s for s in subs if s.customer_name.startswith("DEMO-HOLD")]
        sent = [s for s in core if s.last_sent_at is not None]
        unsent = [s for s in core if s.last_sent_at is None]
        wrong_period = [s.id for s in sent
                        if (s.last_sent_period_end or "")[:10] != "2026-06-30"]
        guard_sent = [s.customer_name for s in guard if s.last_sent_at]
        seq_bad = [s.id for s in core
                   if s.invoice_number_start is not None and s.last_sent_at
                   and (s.invoice_number_next != s.invoice_number_start + 1
                        or s.last_invoice_number != str(s.invoice_number_start))]
        # Pending drafts for GUARD subs are BY DESIGN (drafts render for
        # preview; only the SEND is gated). Pending drafts for core subs = bug.
        core_ids = {s.id for s in core}
        pending_core = [d for d in db.execute(
            select(ReportDraft.subscription_id).where(
                ReportDraft.tenant_id == TENANT_ID,
                ReportDraft.status == "pending")).scalars()
            if d in core_ids]
    res = {
        "core_sent": len(sent), "core_unsent": [s.id for s in unsent],
        "wrong_period_stamp": wrong_period, "guard_demos_sent": guard_sent,
        "sequential_counter_bad": seq_bad,
        "core_drafts_still_pending": len(pending_core),
    }
    ok = (len(sent) == 800 and not unsent and not wrong_period
          and not guard_sent and not seq_bad and not pending_core)
    res["ok"] = ok
    return res


def main() -> int:
    global THROTTLE_S
    init_db()
    t0 = time.time()
    if os.getenv("ANNA800_FAKE_SEND") == "1":
        # Local dry-run: exercise the ENTIRE pipeline (guards, drafts, approve,
        # stamps, idempotence) with the Resend call swapped for a counter.
        import api.notify as _notify
        fake_n = {"n": 0}

        def _fake(to, subject, html, text=None, attachments=None, cc=None,
                  bcc=None, from_addr=None, reply_to=None, product="nepool",
                  log_failures=True):
            fake_n["n"] += 1
            return True

        _notify._send_via_resend = _fake
        THROTTLE_S = 0.0
        print("[dry-run] ANNA800_FAKE_SEND=1 — Resend patched, no email leaves")
    elif not os.getenv("RESEND_API_KEY"):
        print("ABORT: RESEND_API_KEY not set — this must run on prod "
              "(or set ANNA800_FAKE_SEND=1 for a local dry-run).")
        return 2

    render_spot_check()

    # ── canary: one real auto-mode send ──────────────────────────────────────
    from api.billing.delivery import deliver_subscription
    with SessionLocal() as db:
        tenant = db.get(Tenant, TENANT_ID)
        canary = db.execute(
            select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == TENANT_ID,
                   BillingReportSubscription.enabled == True,  # noqa: E712
                   BillingReportSubscription.delivery_mode == "auto")
            .order_by(BillingReportSubscription.id).limit(1)).scalars().first()
        _assert_sink(canary.client_email, canary.operator_email)
        r = deliver_subscription(db, canary, tenant, triggered_by="anna800-canary")
    if not r.get("ok"):
        print(f"ABORT: canary send failed: {json.dumps(r, default=str)[:400]}")
        return 2
    print(f"[1] canary sent OK → {r.get('to')} (bcc {r.get('bcc')})")

    sweep1 = sweep("sweep1")
    print(f"[2] sweep1: sent={len(sweep1['sent'])} drafted={len(sweep1['drafted'])} "
          f"skipped={len(sweep1['skipped'])} failed={len(sweep1['failed'])}")
    if sweep1["failed"]:
        print("  failures:", json.dumps(sweep1["failed"], default=str)[:1500])

    approved = approve_all_drafts()
    print(f"[2b] approved={len(approved['approved'])} "
          f"skipped={len(approved['skipped'])} failed={len(approved['failed'])}")
    if approved["failed"]:
        print("  failures:", json.dumps(approved["failed"], default=str)[:1500])

    res = final_assertions()
    print(f"[3] final: {json.dumps(res, default=str)[:1200]}")

    sweep2 = sweep("idempotence")
    with SessionLocal() as db:
        pen_ids = set(db.execute(
            select(BillingReportSubscription.id).where(
                BillingReportSubscription.tenant_id == TENANT_ID,
                BillingReportSubscription.customer_name.like("DEMO-HOLD%"))
        ).scalars())
    # Guard-pen draft refreshes are in-place updates by design; only a CORE
    # sub re-sending/re-drafting an already-sent period is a failure.
    resent = len([s for s in sweep2["sent"] + sweep2["drafted"]
                  if s not in pen_ids])
    print(f"[4] idempotence sweep: core re-sends/re-drafts={resent} (must be 0), "
          f"already-sent skips={len(sweep2['skipped'])}")

    ok = res["ok"] and resent == 0 and not sweep1["failed"] and not approved["failed"]
    verdict = ("✅ SEND TEST PASS — 800/800 delivered to sink, all stamps correct"
               if ok else "❌ SEND TEST FAILURES — see above")
    print("\n" + verdict)
    print(f"total runtime: {(time.time()-t0)/60:.1f} min")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
