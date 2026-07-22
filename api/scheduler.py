"""
APScheduler — runs pull-bills on a cadence so new monthly bills land
automatically. Also fires per-CLIENT report deliveries based on each
client's report_frequency (falls back to tenant.report_frequency).

Schedule:
  - every 6 hours: enqueue pull_bills jobs for all active tenants
  - every 1 minute: drain the job queue
  - every Monday at 09:00 UTC: deliver to weekly clients
  - 1st of every month at 09:00 UTC: deliver to monthly clients
  - 1st of Jan/Apr/Jul/Oct at 09:00 UTC: deliver to quarterly clients
"""
import atexit
import logging
import os
from datetime import datetime, timedelta

import stripe as stripe

logger = logging.getLogger(__name__)
from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, or_, func, text
from . import branding
from .db import SessionLocal, engine
from .models import Tenant, Client, Array, Job, UtilitySession, now
from .notify import (
    send_add_first_array_email,
    send_payment_failed_email,
    send_trial_charged_email,
    send_trial_charge_failed_email,
    send_trial_paused_no_card_email,
    send_trial_ending_no_card_reminder_email,
    send_internal_alert,
    send_gmp_reauth_needed_email,
)


class _ShutdownSafeExecutor(APSThreadPoolExecutor):
    """Thread-pool executor that tolerates process-exit races.

    On SIGTERM / interpreter teardown, ``concurrent.futures`` shuts down its
    pools via atexit while APScheduler's daemon thread may still call
    ``submit``. That raises ``RuntimeError: cannot schedule new futures after
    shutdown``, which APScheduler logs as ERROR (Sentry noise on every deploy).
    Swallow that specific race; re-raise anything else.
    """

    def submit_job(self, job, run_times):
        try:
            return super().submit_job(job, run_times)
        except RuntimeError as exc:
            if "shutdown" not in str(exc).lower():
                raise
            self._logger.warning(
                "Not submitting job %s — executor already shut down (process exit)",
                getattr(job, "id", job),
            )


# job_defaults: APScheduler's built-in misfire_grace_time is 1 SECOND, so any
# daily cron whose fire moment lands during a restart / GC pause was silently
# marked "missed" and skipped until the next day, with no trace (this is why the
# morning digest went quiet on Ford, 2026-07-17). A 1-hour grace + coalesce means
# a job whose window we were briefly down for still fires exactly once when the
# process comes back, instead of vanishing. Jobs that set their own coalesce/
# max_instances still override these defaults.
scheduler = BackgroundScheduler(
    timezone="UTC",
    job_defaults={"misfire_grace_time": 3600, "coalesce": True},
    executors={"default": _ShutdownSafeExecutor()},
)

_atexit_stop_registered = False


def scheduler_enabled() -> bool:
    """Whether this process should own APScheduler + Sovereign ticks.

    RUN_SCHEDULER defaults to **OFF** (fail-safe for public web). Explicitly set
    RUN_SCHEDULER=1 on the worker. A cleared env var must NOT silently collapse
    the web/worker split back to a single process that runs harvest schedules.
    """
    v = (os.environ.get("RUN_SCHEDULER") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def enqueue_pull_for_all_tenants():
    with SessionLocal() as db:
        # Never enqueue a real-utility pull for the public demo tenant — its
        # ~828 fabricated GMP accounts only ever 403, and the sweep saturates
        # the web workers so no request can be served (took prod down
        # 2026-07-05). Mirrors usage_report / freshness_scorecard, which
        # already exclude is_demo.
        tenants = db.execute(
            select(Tenant).where(Tenant.active == True, Tenant.is_demo.is_(False))
        ).scalars().all()
        for t in tenants:
            db.add(Job(tenant_id=t.id, kind="pull_bills", payload={}, status="queued"))
        db.commit()
    return len(tenants)


def reconcile_warranty_claims() -> dict:
    """Watch every Array Operator owner's fleet: auto-open claims for newly
    dead/faulted inverters, auto-close ones that recovered, and fire any
    grace-timer auto-sends that have come due. This is what makes the claims
    'automatic' — the owner never has to open the tab for the agent to act."""
    from . import warranty_claims
    opened = closed = sent = touched = 0
    errors = 0
    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(Tenant.active == True, Tenant.product == "array_operator")
        ).scalars().all()
    for t in tenants:
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, t.id)
                tally = warranty_claims.reconcile(db, tenant)
                sent += warranty_claims.process_due(db, tenant)
                opened += tally["opened"]
                closed += tally["closed"]
                touched += 1
        except Exception as exc:  # one bad fleet pull must not stall the rest
            errors += 1
            logger.warning("warranty reconcile failed for %s: %s", t.id, exc)
    result = {"tenants": touched, "opened": opened, "closed": closed,
              "auto_sent": sent, "errors": errors}
    if opened or sent:
        logger.info("warranty claims: %s", result)
    return result


def reconcile_repair_ops() -> dict:
    """O&M healing: open repair tickets for dead/fault units with a known
    service contact, clear recovered tickets, fire due auto check-ins."""
    from . import repair_ops
    opened = closed = sent = touched = escalated = 0
    errors = 0
    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(Tenant.active == True, Tenant.product == "array_operator")
        ).scalars().all()
    for t in tenants:
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, t.id)
                tally = repair_ops.reconcile(db, tenant)
                sent += repair_ops.process_due(db, tenant)
                # Week-long-down → email the owner for action + a repair contact
                # (idempotent via owner_escalated_at).
                escalated += repair_ops.escalate_stale_repairs(db, tenant)
                opened += tally.get("opened", 0) or 0
                closed += tally.get("closed", 0) or 0
                touched += 1
        except Exception as exc:
            errors += 1
            logger.warning("repair ops reconcile failed for %s: %s", t.id, exc)
    result = {"tenants": touched, "opened": opened, "closed": closed,
              "auto_checkins": sent, "owner_escalations": escalated, "errors": errors}
    if opened or sent or escalated:
        logger.info("repair ops: %s", result)
    return result


def _build_audit_for_tenant(db, tenant_id: str) -> dict:
    """Run the settlement audit across one tenant's fleet → the same summary
    shape the Audit tab uses. Read-only; never fabricates a verdict."""
    from .reconciliation import reconcile_array
    from .models import Array
    rows: list[dict] = []
    s = {"total": 0, "auditable": 0, "ok": 0, "leak": 0, "leak_unconfirmed": 0,
         "incomplete_monitoring": 0, "insufficient_data": 0,
         "have_settlement": 0, "have_production": 0, "dollars_flagged": 0.0}
    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant_id,
                            Array.deleted_at.is_(None), Array.excluded.is_(False))
        .order_by(Array.id)
    ).scalars().all()
    for arr in arrays:
        s["total"] += 1
        try:
            r = reconcile_array(db, arr.id)
        except Exception:
            s["insufficient_data"] += 1
            continue
        if r.status in s:
            s[r.status] += 1
        if r.status in ("ok", "leak"):
            s["auditable"] += 1
        if r.settlement_kwh > 0:
            s["have_settlement"] += 1
        if r.production_kwh > 0:
            s["have_production"] += 1
        if r.status in ("leak", "leak_unconfirmed"):
            s["dollars_flagged"] += r.dollars_at_risk or 0.0
            rows.append({"name": arr.name, "status": r.status,
                         "variance_pct": r.variance_pct,
                         "dollars": r.dollars_at_risk or 0.0,
                         "headline": r.notes[-1] if r.notes else ""})
    s["dollars_flagged"] = round(s["dollars_flagged"], 2)
    s["coverage_pct"] = (round(100.0 * s["auditable"] / s["total"], 1)
                         if s["total"] else 0.0)
    rows.sort(key=lambda x: (0 if x["status"] == "leak" else 1, -x["dollars"]))
    return {"summary": s, "flagged": rows}


def deliver_weekly_audit_digest() -> dict:
    """Weekly settlement-audit digest to every active Array Operator CLIENT.

    Runs the production-vs-settlement audit across each owner's fleet and emails
    them a plain-language summary: dollars flagged, what reconciles, what needs a
    monitoring connection to confirm, and how much of the fleet is auditable.
    Honest by construction — sends even when there's nothing flagged ('all clear'),
    and never invents a leak. Skips owners we can't email or whose fleet has no
    bills to audit yet (nothing useful to say)."""
    import html as _html
    from .models import Tenant
    from .notify import _send_via_resend
    from .email_skin import render_email_skin, render_email_skin_text

    DASH = "https://arrayoperator.com/#audit"
    sent = skipped = errors = 0
    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(
                Tenant.active == True, Tenant.product == "array_operator")  # noqa: E712
        ).scalars().all()
        tids = [(t.id, t.contact_email, (t.operator_name or t.company_name or t.name or "there"))
                for t in tenants]

    for tid, email, name in tids:
        if not email:
            skipped += 1
            continue
        try:
            with SessionLocal() as db:
                data = _build_audit_for_tenant(db, tid)
            s = data["summary"]
            # Nothing to audit yet (no bills anywhere) → don't email noise.
            if not s["total"] or s["have_settlement"] == 0:
                skipped += 1
                continue

            first = _html.escape(str(name).split()[0])
            flagged_n = s["leak"] + s["leak_unconfirmed"]
            dollars = f"${s['dollars_flagged']:,.0f}"

            # Headline line: dollars when flagged, else an all-clear.
            if flagged_n:
                intro = (f"We found {flagged_n} array{'s' if flagged_n != 1 else ''} "
                         f"worth a look this week — {dollars} flagged.")
                preheader = f"{dollars} flagged across {flagged_n} array(s) this week."
            else:
                intro = "Your fleet reconciles cleanly this week — nothing flagged."
                preheader = "Your weekly settlement audit: all clear."

            # Flagged rows table (only the concerning ones).
            rows_html = ""
            for r in data["flagged"][:12]:
                tag = ("Leak" if r["status"] == "leak" else "Unconfirmed gap")
                color = "#ff6b6b" if r["status"] == "leak" else "#c07d2a"
                var = (f"{r['variance_pct']:+.0f}%" if r["variance_pct"] is not None else "")
                rows_html += (
                    f'<tr><td style="padding:8px 0;border-bottom:1px solid #e5ebf1;">'
                    f'<strong>{_html.escape(r["name"])}</strong> '
                    f'<span style="color:{color};font-weight:700;font-size:13px;">{tag}</span> '
                    f'<span style="color:#5a6675;font-size:13px;">{var}</span></td>'
                    f'<td style="padding:8px 0;border-bottom:1px solid #e5ebf1;text-align:right;'
                    f'font-weight:700;color:{color};">${r["dollars"]:,.0f}</td></tr>')
            table_html = (
                f'<table style="width:100%;border-collapse:collapse;margin:14px 0;">'
                f'{rows_html}</table>') if rows_html else ""

            body_html = (
                f"<p>Hi {first},</p>"
                f"<p>Here's your weekly settlement audit — we reconcile what your "
                f"arrays actually produced against what the utility settled, and flag "
                f"any gaps in plain dollars.</p>"
                f"{table_html}"
                f'<p style="background:#f5f8fb;border-radius:8px;padding:14px 16px;font-size:14px;color:#33414f;">'
                f"<strong>{s['auditable']} of {s['total']}</strong> arrays fully auditable "
                f"({s['coverage_pct']:.0f}%) · "
                f"<strong>{s['have_settlement']}</strong> with utility bills · "
                f"<strong>{s['have_production']}</strong> with a production feed.</p>"
                + ("<p>An <em>unconfirmed gap</em> means the meter data diverges from "
                   "your bill, but we only have the utility's own figure — connect your "
                   "inverter monitoring to confirm it as a real, recoverable leak.</p>"
                   if s["leak_unconfirmed"] else "")
                + f'<p style="margin-top:22px;font-size:14px;opacity:.85;">'
                  f"See every array's verdict on your "
                  f'<a href="{DASH}" style="color:#3fd68a;">Audit dashboard</a>.</p>'
                  f'<p style="margin-top:20px;">— Array Operator</p>'
            )
            body_text = (
                f"Hi {str(name).split()[0]},\n\n"
                f"Weekly settlement audit:\n"
                + (f"  {dollars} flagged across {flagged_n} array(s).\n" if flagged_n
                   else "  All clear — nothing flagged.\n")
                + "".join(
                    f"  - {r['name']}: {'Leak' if r['status']=='leak' else 'Unconfirmed gap'} "
                    f"(${r['dollars']:,.0f})\n" for r in data["flagged"][:12])
                + f"\n{s['auditable']} of {s['total']} arrays auditable "
                  f"({s['coverage_pct']:.0f}%). "
                  f"{s['have_settlement']} with bills, {s['have_production']} with a "
                  f"production feed.\n\n"
                  f"See full details: {DASH}\n\n— Array Operator"
            )

            subject = (f"Settlement audit: {dollars} flagged this week"
                       if flagged_n else "Weekly settlement audit — all clear")
            html = render_email_skin(
                preheader=preheader, headline="Array Operator",
                intro_line=intro, body_html=body_html,
                cta={"label": "Open your Audit dashboard", "url": DASH},
                product="array_operator")
            text = render_email_skin_text(
                headline="Array Operator", intro_line=intro, body_text=body_text,
                cta={"label": "Open your Audit dashboard", "url": DASH},
                product="array_operator")

            if _send_via_resend(to=email, subject=subject, html=html, text=text,
                                product="array_operator"):
                sent += 1
            else:
                errors += 1
        except Exception as exc:  # one bad fleet must not stall the batch
            errors += 1
            logger.warning("audit digest failed for %s: %s", tid, exc)

    result = {"sent": sent, "skipped": skipped, "errors": errors}
    logger.info("weekly audit digest: %s", result)
    return result


def _deliver_clients_with_frequency(frequency: str) -> dict:
    """Send the workbook to every active CLIENT whose effective frequency
    matches. Effective = client.report_frequency if set, else
    tenant.report_frequency. Skips clients of inactive non-comped tenants.
    """
    from .delivery import deliver_for_client
    from .notify import send_internal_alert
    from .jobs.report_digests import record_scheduled_batch
    from .report_eligibility import tenant_reports_eligible

    sent: list[int] = []
    failed: list[int] = []
    skipped_empty: list[int] = []
    results: list[dict] = []  # full per-client outcome → operator delivery receipt
    with SessionLocal() as db:
        # All LIVE client rows that EITHER explicitly match the cadence OR
        # inherit it from the tenant. (deleted_at filter mirrors
        # run_presend_reviews — a soft-deleted client must never be scheduled.)
        rows = db.execute(
            select(Client, Tenant)
            .join(Tenant, Client.tenant_id == Tenant.id)
            .where(Client.active == True)  # noqa: E712
            .where(Client.deleted_at.is_(None))
            # AUTO-SEND ENROLLMENT (THE FOLD): only clients the operator explicitly
            # enrolled auto-send (and so consent to the $15/quarter on first output).
            # Auto-propagated capture-artifact clients default auto_send=False and are
            # never auto-sent/charged; legacy NEPOOL clients were backfilled True
            # (migrate.py) so their existing auto-sends continue. Manual sends +
            # downloads still work for any client regardless of this flag.
            .where(Client.auto_send == True)  # noqa: E712
            .where(
                or_(
                    Client.report_frequency == frequency,
                    (Client.report_frequency.is_(None)) &
                    (Tenant.report_frequency == frequency),
                )
            )
        ).all()
        # Data-presence eligibility (THE FOLD): the join IS the data test —
        # this tenant has an active client on this cadence. The predicate adds
        # standing (active/comped/trialing, same as before) and excludes the
        # demo tenant (whose seeded clients previously churned through as
        # no-recipient no-ops every quarter). NO product gate: a migrated
        # Array Operator tenant with clients + a cadence is picked up.
        candidates = [c.id for (c, t) in rows if tenant_reports_eligible(t)]
        # name + tenant per candidate, so a client that RAISES mid-send still
        # lands a labeled row in the operator's receipt.
        cand_set = set(candidates)
        cand_meta = {c.id: (c.name, t.id) for (c, t) in rows if c.id in cand_set}

    for cid in candidates:
        try:
            # skip_if_empty: never auto-email a blank workbook (a client with
            # arrays but no bills/daily data, or an empty onboarding stub).
            result = deliver_for_client(cid, triggered_by=f"sched-{frequency}",
                                        skip_if_empty=True)
            if result.get("skipped_empty"):
                skipped_empty.append(cid)
            elif result.get("ok") and result.get("email_sent"):
                sent.append(cid)
            else:
                failed.append(cid)
            results.append(result)
        except Exception as e:
            failed.append(cid)
            cname, ctid = cand_meta.get(cid, (f"client {cid}", None))
            results.append({"ok": False, "client_id": cid, "client_name": cname,
                            "tenant": ctid, "error": str(e),
                            "reason": "report generation failed"})
            send_internal_alert(
                f"Scheduled delivery failed ({frequency})",
                f"Client: {cid}\nError: {e}",
            )

    # Persist the batch so the post-send receipt job can confirm delivery and
    # report skips/failures to the operator. Best-effort: logging must never
    # break the actual delivery run.
    try:
        record_scheduled_batch(frequency, results)
    except Exception as e:  # noqa: BLE001
        logger.warning("record_scheduled_batch failed (%s): %s", frequency, e)

    # Operator NEPOOL-GIS directory: one workbook with every client array that
    # was in this batch, emailed only to each NEPOOL tenant operator so they
    # can bulk-upload to the NEPOOL site. Group by tenant from result rows.
    try:
        from .delivery import deliver_operator_directory
        by_tenant: dict[str, list[int]] = {}
        for r in results:
            tid = r.get("tenant") or r.get("tenant_id")
            cid = r.get("client_id")
            if not tid or cid is None:
                continue
            # Include clients that sent OR had data skipped — directory still
            # only includes arrays with generation in the window.
            if r.get("ok") or r.get("skipped_empty"):
                by_tenant.setdefault(str(tid), []).append(int(cid))
        for tid, cids in by_tenant.items():
            try:
                deliver_operator_directory(
                    tid,
                    client_ids=sorted(set(cids)),
                    triggered_by=f"sched-{frequency}-directory",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "scheduled directory failed tenant=%s: %s", tid, e,
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduled directory fan-out failed (%s): %s", frequency, e)

    # Internal-alert on failures AND on skipped-empty clients, so the operator
    # learns which clients have no data instead of silently sending nothing.
    if failed or skipped_empty:
        send_internal_alert(
            f"Scheduled delivery — {frequency} run summary",
            f"Sent OK: {sent}\nFailed: {failed}\n"
            f"Skipped (no generation data, not emailed): {skipped_empty}",
        )
    return {"frequency": frequency, "sent": sent, "failed": failed,
            "skipped_empty": skipped_empty}


def deliver_weekly_reports():
    return _deliver_clients_with_frequency("weekly")


def deliver_monthly_reports():
    return _deliver_clients_with_frequency("monthly")


def deliver_quarterly_reports():
    return _deliver_clients_with_frequency("quarterly")


def _auto_send_should_hold(db, sub) -> bool:
    """True when an offtaker's invoice carries a GENUINE "doesn't match GMP"
    allocation flag — the same reconcile signal the operator sees in the pipeline.

    Used to HOLD an unattended AUTO-send (sim #5b): a scheduled send must never push
    an invoice whose share-of-excess doesn't reconcile against GMP's own credit under
    the operator's name. Data-quality artifacts (missing/prorated/unmetered data,
    period-timing gaps) are already reclassified to non-mismatch statuses upstream, so
    they never hold — only a real reconciled delta does.

    Fail-OPEN: any error computing the reconcile returns False (don't hold). An
    inconclusive check must not halt the pipeline — today's auto-send fires with no
    check at all, so this only ever ADDS a hold for the known-flagged. Manual/approval
    sends never reach here, so a human "Approve & send" can always override."""
    try:
        from .billing.reconcile_bills import reconcile_subscription, is_gmp_offtaker
        # Bill audit / auto-send hold is GMP group-net-metering only.
        if not is_gmp_offtaker(db, sub):
            return False
        rec = reconcile_subscription(db, sub)
    except Exception:  # noqa: BLE001 — never let the safety check break the run
        return False
    alloc = rec.get("allocation") or {}
    return alloc.get("status") == "mismatch"


def deliver_billing_reports(cadence: str, *, trueup_only: bool = False) -> dict:
    """Array Operator automatic billing reports — deliver every enabled
    BillingReportSubscription whose cadence matches (or, for the annual run,
    every sub with annual_trueup set).

    Annual true-up (trueup_only=True) uses the real budget-vs-actual settlement
    path (api.billing.trueup): charge underpayment or bank credit for the next
    bill — not a single month's invoice mislabeled as a true-up.

    Mirrors _deliver_clients_with_frequency: skips subs of inactive non-comped
    tenants, internal-alerts on failure, exactly-once stamping happens inside
    deliver_subscription (it sets last_sent_at / next_send_at on success only)."""
    from .models import BillingReportSubscription
    from .billing.delivery import (
        deliver_subscription, draft_subscription,
        deliver_trueup_subscription, draft_trueup_subscription,
    )

    sent: list[int] = []
    drafted: list[int] = []
    failed: list[int] = []
    skipped: list[int] = []   # benign no-ops (waiting on a bill, or already sent
                              # this period) — NOT failures; don't alert on them.
    held: list[int] = []      # auto-send withheld: the invoice doesn't match GMP's
                              # bill (reconcile flag) — awaits operator review (sim #5b).
    with SessionLocal() as db:
        q = (
            select(BillingReportSubscription, Tenant)
            .join(Tenant, BillingReportSubscription.tenant_id == Tenant.id)
            .where(BillingReportSubscription.enabled == True)  # noqa: E712
            .where(BillingReportSubscription.deleted_at.is_(None))
        )
        if trueup_only:
            q = q.where(BillingReportSubscription.annual_trueup == True)  # noqa: E712
        else:
            q = q.where(BillingReportSubscription.cadence == cadence)
        rows = db.execute(q).all()
        candidates = [
            sub.id for (sub, t) in rows
            if (t.active or t.subscription_status in ("comped", "trialing"))
        ]

        for sid in candidates:
            sub = db.get(BillingReportSubscription, sid)
            tenant = db.get(Tenant, sub.tenant_id) if sub else None
            if sub is None or tenant is None:
                continue
            # Send-pipeline pause switch: the operator halted SCHEDULED runs —
            # no auto sends, no auto drafts, until they resume. Manual sends
            # and draft approvals still work (pause stops the machine, not
            # the operator). Benign skip — never alerts.
            if getattr(tenant, "sending_paused", False):
                skipped.append(sid)
                continue
            # Per-customer choice: "approval" (default) drafts into the operator's
            # inbox for review-and-send; "auto" sends straight to the recipient.
            mode = getattr(sub, "delivery_mode", "approval") or "approval"
            try:
                if trueup_only:
                    # Real year-end settlement (budget vs actual). Works for
                    # utility-bill offtakers too — that was the #18 gap.
                    if mode == "auto":
                        result = deliver_trueup_subscription(
                            db, sub, tenant,
                            triggered_by="sched-billing-trueup")
                        if result.get("ok"):
                            sent.append(sid)
                        elif result.get("skipped"):
                            skipped.append(sid)
                        else:
                            failed.append(sid)
                    else:
                        result = draft_trueup_subscription(
                            db, sub, tenant,
                            triggered_by="sched-draft-trueup")
                        if result.get("ok"):
                            drafted.append(sid)
                        elif result.get("skipped"):
                            skipped.append(sid)
                        else:
                            failed.append(sid)
                elif mode == "auto":
                    # SAFETY (sim #5b): never auto-FIRE an invoice that doesn't reconcile
                    # against GMP's own bill. The genuine "doesn't match GMP" allocation
                    # flag the operator sees in the pipeline HOLDS the unattended send until
                    # they review it — a scheduled send must not push a known-wrong invoice
                    # under the operator's name. A human "Approve & send" is unaffected. The
                    # next scheduled run re-checks, so a resolved flag sends automatically.
                    if _auto_send_should_hold(db, sub):
                        held.append(sid)
                        logger.info(
                            "auto-send HELD for sub %s — invoice doesn't match GMP's bill "
                            "(allocation mismatch); awaiting operator review", sid)
                        continue
                    result = deliver_subscription(
                        db, sub, tenant,
                        triggered_by=f"sched-billing-{cadence}")
                    if result.get("ok"):
                        sent.append(sid)
                    elif result.get("skipped"):
                        skipped.append(sid)   # waiting on bill / already sent — benign
                    else:
                        failed.append(sid)
                else:
                    result = draft_subscription(
                        db, sub, tenant,
                        triggered_by=f"sched-draft-{cadence}")
                    if result.get("ok"):
                        drafted.append(sid)
                    elif result.get("skipped"):
                        skipped.append(sid)
                    else:
                        failed.append(sid)
                if not result.get("ok"):
                    logger.info("billing delivery %s sub %s: %s",
                                "skipped" if result.get("skipped") else "FAILED",
                                sid, result.get("error"))
            except Exception as e:  # noqa: BLE001
                failed.append(sid)
                send_internal_alert(
                    f"Array Operator billing delivery failed ({cadence})",
                    f"Subscription: {sid}\nError: {e}",
                )

    if failed:
        send_internal_alert(
            f"Array Operator billing — partial failures ({cadence})",
            f"Sent OK: {sent}\nDrafted: {drafted}\nSkipped (benign): {skipped}\n"
            f"Held (doesn't match GMP — review): {held}\nFailed: {failed}",
        )
    return {"cadence": cadence, "trueup_only": trueup_only,
            "sent": sent, "drafted": drafted, "failed": failed,
            "skipped": skipped, "held": held}


def deliver_monthly_billing_reports() -> dict:
    return deliver_billing_reports("monthly")


def deliver_quarterly_billing_reports() -> dict:
    return deliver_billing_reports("quarterly")


def deliver_annual_billing_trueups() -> dict:
    return deliver_billing_reports("annual", trueup_only=True)


def finalize_expired_trials():
    """Convert expired trials to live subscriptions (or extend zero-array trials).

    Runs hourly. For each tenant in 'trialing' state whose trial_ends_at has
    passed:
      - If they have no arrays yet and haven't been extended: add 3 days,
        send the 'add your first array' email, and leave them trialing.
      - Otherwise: create the Stripe subscription on the stored payment method
        (quantity = actual array count, minimum 1), mark active, clear trial.
    """
    stripe_secret = os.getenv("STRIPE_SECRET_KEY", "")
    setup_price_id = os.getenv("STRIPE_SETUP_PRICE_ID", "")
    # Per-array price id is resolved per-tenant by product below
    # (array_price_id_for_product) — NEPOOL gets the per-array price, Array
    # Operator gets the per-kWh metered price.

    if not stripe_secret:
        return  # not configured — skip silently

    stripe.api_key = stripe_secret

    cutoff = datetime.utcnow()
    with SessionLocal() as db:
        trialing = db.execute(
            select(Tenant).where(
                Tenant.trial_ends_at <= cutoff,
                Tenant.subscription_status == "trialing",
            )
        ).scalars().all()

        for t in trialing:
            array_count = db.execute(
                select(func.count()).select_from(Array).where(
                    Array.tenant_id == t.id,
                    Array.deleted_at.is_(None),
                    Array.excluded.is_(False),
                )
            ).scalar() or 0

            if array_count == 0 and not t.trial_extended:
                t.trial_ends_at = t.trial_ends_at + timedelta(days=3)
                t.trial_extended = True
                db.commit()
                try:
                    _product = getattr(t, "product", "nepool") or "nepool"
                    send_add_first_array_email(
                        to=t.contact_email, name=t.operator_name or t.company_name or t.name,
                        dashboard_url=branding.dashboard_url(_product),
                        product=_product)
                except Exception:
                    pass
                send_internal_alert(
                    f"Trial extended (no arrays): {t.id}",
                    f"Tenant {t.id} ({t.contact_email}) had 0 arrays at trial end. "
                    f"Extended 3 days."
                )
                continue

            # No-upfront-payment: if the operator never added a card, we can't
            # charge them. Auto-pause instead of failing — keep the tenant alive,
            # flip to read-only, stop sending reports. They can add a card from
            # the dashboard any time and resume. This check comes AFTER the
            # zero-arrays grace so a card-less operator with no arrays still gets
            # the 3-day extension first.
            if not t.stripe_payment_method_id:
                t.subscription_status = "paused_no_card"
                t.trial_ends_at = None
                t.active = False  # gates report delivery (see filters below)
                db.commit()
                try:
                    _product = getattr(t, "product", "nepool") or "nepool"
                    send_trial_paused_no_card_email(
                        to=t.contact_email,
                        name=t.operator_name or t.company_name or t.name,
                        dashboard_url=branding.dashboard_url(_product),
                        product=_product)
                except Exception as mail_err:
                    logger.warning("send_trial_paused_no_card_email failed: %s", mail_err)
                send_internal_alert(
                    f"Trial paused (no card): {t.id}",
                    f"Tenant {t.id} ({t.contact_email}) reached trial end with no "
                    f"card on file. Paused (read-only), {array_count} arrays held. "
                    f"Nothing deleted — they can add a card to resume."
                )
                continue

            # Charge the card.
            quantity = max(array_count, 1)
            product = getattr(t, "product", "nepool")
            try:
                from .stripe_helpers import (
                    array_price_id_for_product, is_array_operator, ao_monitoring_item,
                )
                ao = is_array_operator(product)
                price_id = array_price_id_for_product(product)
                items = []
                add_invoice_items = []
                if ao:
                    # Array Operator monitoring = per-kW NAMEPLATE line (quantity =
                    # registered nameplate kW; the daily nameplate-sync keeps it
                    # current). No setup fee.
                    mi = ao_monitoring_item(db, t.id)
                    if mi:
                        items.append(mi)
                else:
                    if price_id:
                        items.append({"price": price_id, "quantity": quantity})
                    # $250 setup is a ONE-TIME price → Stripe rejects it in
                    # subscription `items` (must be recurring); bill it on the
                    # first invoice via add_invoice_items instead. (Same fix as
                    # stripe_helpers.create_subscription_for_tenant — without it
                    # every NEPOOL trial-end conversion raised InvalidRequestError
                    # and silently failed to convert.)
                    if setup_price_id:
                        add_invoice_items.append({"price": setup_price_id, "quantity": 1})
                sub = stripe.Subscription.create(
                    customer=t.stripe_customer_id,
                    items=items if items else None,
                    default_payment_method=t.stripe_payment_method_id,
                    add_invoice_items=add_invoice_items or None,
                )
                # Stripe SDK v15 returns StripeObjects without .get(); use [] with `in`.
                sub_dict = sub.to_dict() if hasattr(sub, "to_dict") else sub
                sub_id = sub_dict["id"]
                t.stripe_subscription_id = sub_id
                t.subscription_status = "active"
                t.trial_ends_at = None
                db.commit()

                # Estimate the charge for the confirmation email.
                amount_dollars = 0.0
                latest_inv_id = sub_dict.get("latest_invoice") if hasattr(sub_dict, "get") else (
                    sub_dict["latest_invoice"] if "latest_invoice" in sub_dict else None
                )
                try:
                    if latest_inv_id:
                        inv = stripe.Invoice.retrieve(latest_inv_id)
                        inv_dict = inv.to_dict() if hasattr(inv, "to_dict") else inv
                        amount_dollars = (inv_dict.get("amount_due") or 0) / 100
                except Exception:
                    pass

                try:
                    send_trial_charged_email(
                        to=t.contact_email, name=t.operator_name or t.company_name or t.name,
                        array_count=quantity, amount_dollars=amount_dollars, product=product)
                except Exception:
                    pass
                send_internal_alert(
                    f"Trial ended — charged {t.id}",
                    f"Tenant {t.id} ({t.contact_email}) trial ended. "
                    f"Arrays: {array_count}, billed qty: {quantity}. "
                    f"Subscription: {sub_id}"
                )
            except Exception as e:
                send_internal_alert(
                    f"Trial-end charge FAILED: {t.id}",
                    f"Tenant {t.id} ({t.contact_email}) could not be charged at trial end.\n"
                    f"Arrays: {array_count}, pm: {t.stripe_payment_method_id}\n"
                    f"Error: {e}\nManual intervention needed."
                )
                try:
                    send_trial_charge_failed_email(
                        to=t.contact_email, name=t.operator_name or t.company_name or t.name,
                        dashboard_url=branding.dashboard_url(product),
                        product=product)
                except Exception as mail_err:
                    logger.warning("send_trial_charge_failed_email failed: %s", mail_err)


def send_trial_ending_reminders() -> dict:
    """Nudge no-card trialing operators to add a card before their trial ends.

    Two exactly-once touches, each gated by its own timestamp so a missed or
    double-fired tick never drops or duplicates a send:
      EARLY  ~7 days out  -> stamps trial_reminder_sent_at
      URGENT ~2 days out  -> stamps trial_final_reminder_sent_at (after EARLY)
    Tenants who add a card go through the normal trial-charge path and drop out.
    Runs once daily.
    """
    if not os.getenv("STRIPE_SECRET_KEY"):
        # Mirrors finalize_expired_trials: skip when billing isn't configured.
        return {"reminded": [], "urgent": []}
    now = datetime.utcnow()
    early_window = now + timedelta(days=7)
    urgent_window = now + timedelta(days=2)
    reminded: list[str] = []
    urgent: list[str] = []

    def _send(db, stage_field: str, rows, bucket: list[str]) -> None:
        targets = [(t.id, t.contact_email,
                    t.operator_name or t.company_name or t.name,
                    t.trial_ends_at, getattr(t, "product", "nepool")) for t in rows]
        for tid, email, name, trial_ends_at, product in targets:
            try:
                end_str = trial_ends_at.strftime(
                    f"%B {trial_ends_at.day}, {trial_ends_at.year}")
                send_trial_ending_no_card_reminder_email(
                    to=email, name=name, trial_end_date=end_str,
                    dashboard_url=branding.dashboard_url(product), product=product)
                # Stamp only on a successful send so a transient email failure
                # leaves the tenant eligible next tick (at-least-once on failure,
                # exactly-once on success).
                t = db.get(Tenant, tid)
                if t is not None:
                    setattr(t, stage_field, datetime.utcnow())
                    db.commit()
                bucket.append(tid)
            except Exception as e:
                db.rollback()
                logger.warning(
                    "send_trial_ending_no_card_reminder_email (%s) failed for %s: %s",
                    stage_field, tid, e)

    with SessionLocal() as db:
        early_rows = db.execute(
            select(Tenant).where(
                Tenant.subscription_status == "trialing",
                Tenant.stripe_payment_method_id.is_(None),
                Tenant.trial_ends_at <= early_window,
                Tenant.trial_reminder_sent_at.is_(None),
            )
        ).scalars().all()
        _send(db, "trial_reminder_sent_at", early_rows, reminded)

        urgent_rows = db.execute(
            select(Tenant).where(
                Tenant.subscription_status == "trialing",
                Tenant.stripe_payment_method_id.is_(None),
                Tenant.trial_ends_at <= urgent_window,
                Tenant.trial_final_reminder_sent_at.is_(None),
                Tenant.trial_reminder_sent_at.is_not(None),
            )
        ).scalars().all()
        # A tenant whose trial ends within the URGENT window (<=2d) also matches
        # the EARLY window (<=7d), so it just got its EARLY reminder stamped
        # above. Skip it here so it doesn't receive BOTH touches in one sweep —
        # the URGENT nudge is a distinct later tick, fired once the EARLY stamp
        # was set on a PRIOR run. Without this guard a <=2d no-card tenant gets
        # two identical reminder emails in a single pass.
        urgent_rows = [t for t in urgent_rows if t.id not in reminded]
        _send(db, "trial_final_reminder_sent_at", urgent_rows, urgent)

    logger.info("send_trial_ending_reminders: early=%d urgent=%d",
                len(reminded), len(urgent))
    return {"reminded": reminded, "urgent": urgent}


# Consecutive GMP-refresh failures before we email the operator to re-login. We
# notify exactly ONCE per outage (on the run that crosses this count), never every
# hourly tick — see the transition check in refresh_expiring_gmp_tokens.
_GMP_REAUTH_NOTIFY_AFTER = 3


def refresh_expiring_gmp_tokens() -> dict:
    """Refresh GMP sessions expiring within 7 days.

    Runs hourly. Safe to call more frequently — refresh is idempotent.
    After 3 consecutive failures, sends the operator a re-auth email and
    logs an internal alert.
    """
    from .gmp_refresh import refresh_gmp_token, GmpRefreshError

    refreshed: list[int] = []
    failed: list[int] = []
    skipped: int = 0
    cutoff = datetime.utcnow() + timedelta(days=7)

    with SessionLocal() as db:
        sessions = db.execute(
            select(UtilitySession).where(
                UtilitySession.provider == "gmp",
                UtilitySession.refresh_token.isnot(None),
                UtilitySession.expires_at <= cutoff,
            )
        ).scalars().all()

        # Tenant-level reauth-email de-dup: only the AUTHORITATIVE (newest-
        # captured) GMP session per tenant may trigger a reauth email. Until the
        # dup-session consolidation runs, an operator can have several stale GMP
        # session rows for the same login (prod 2026-06-21: up to 6) — each one
        # keeps failing to refresh, and without this gate ONE outage fans out
        # into one email PER dup row. The newest capture is the row selection
        # actually uses (latest-per-provider in api/sessions.py), so it is the
        # only one whose failure means the operator genuinely must re-auth; the
        # older zombies are ignored for notification. (The per-session crossing
        # test below still prevents hourly re-spam from the authoritative row.)
        tenant_ids = {s.tenant_id for s in sessions}
        authoritative_sess_id: dict[str, int] = {}
        if tenant_ids:
            newest_rows = db.execute(
                select(UtilitySession.tenant_id, UtilitySession.id)
                .where(UtilitySession.provider == "gmp",
                       UtilitySession.tenant_id.in_(tenant_ids))
                .order_by(UtilitySession.tenant_id,
                          UtilitySession.captured_at.desc(),
                          UtilitySession.id.desc())
            ).all()
            for _tid, _sid in newest_rows:
                authoritative_sess_id.setdefault(_tid, _sid)  # first per tenant = newest

        for sess in sessions:
            tenant = db.get(Tenant, sess.tenant_id)
            token_prefix = sess.refresh_token[:8] if sess.refresh_token else "?"
            try:
                new_jwt, new_expires_at = refresh_gmp_token(sess.refresh_token)
                sess.api_token = new_jwt
                sess.expires_at = new_expires_at
                sess.captured_at = datetime.utcnow()
                sess.last_refresh_at = datetime.utcnow()
                sess.refresh_failures = 0
                db.commit()
                logger.info(
                    "GMP session refreshed: tenant=%s sess=%d token_prefix=%s...",
                    sess.tenant_id, sess.id, token_prefix,
                )
                refreshed.append(sess.id)
            except GmpRefreshError as exc:
                prev_failures = sess.refresh_failures or 0
                sess.refresh_failures = prev_failures + 1
                db.commit()
                logger.warning(
                    "GMP refresh failed: tenant=%s sess=%d failures=%d err=%s",
                    sess.tenant_id, sess.id, sess.refresh_failures, exc,
                )
                failed.append(sess.id)
                # Notify the operator ONCE per outage — on the run that CROSSES the
                # failure threshold — never every hourly tick thereafter. The old
                # '>= 3' re-sent the reauth email on EVERY subsequent run, so a
                # genuinely-revoked GMP session emailed the owner hourly forever
                # (seen in prod: sessions reached 20-70+ failures = that many
                # duplicate emails to one owner). The transition test (prev < N <=
                # now) is independent of the increment size; a successful refresh
                # resets failures to 0 above, re-arming a fresh notice next incident.
                crossed_threshold = (
                    prev_failures < _GMP_REAUTH_NOTIFY_AFTER <= sess.refresh_failures
                )
                is_authoritative = (
                    sess.id == authoritative_sess_id.get(sess.tenant_id)
                )
                _last_alert = tenant.gmp_reauth_alert_at if tenant else None
                _cooldown_ok = (_last_alert is None) or (datetime.utcnow() - _last_alert >= timedelta(days=7))
                if crossed_threshold and is_authoritative and tenant and _cooldown_ok:
                    try:
                        send_gmp_reauth_needed_email(
                            to=tenant.contact_email,
                            name=tenant.operator_name or tenant.company_name or tenant.name,
                            product=tenant.product,
                        )
                    except Exception as notify_exc:
                        logger.error(
                            "Failed to send reauth email to %s: %s",
                            tenant.contact_email, notify_exc,
                        )
                    send_internal_alert(
                        f"GMP refresh: {_GMP_REAUTH_NOTIFY_AFTER} consecutive failures for tenant {sess.tenant_id}",
                        f"Tenant: {sess.tenant_id} ({getattr(tenant, 'contact_email', '?')})\n"
                        f"Session: {sess.id}\nToken prefix: {token_prefix}...\n"
                        f"Operator notified to re-login.",
                    )
                    tenant.gmp_reauth_alert_at = datetime.utcnow()
                    db.commit()

    logger.info(
        "refresh_expiring_gmp_tokens: refreshed=%d failed=%d skipped=%d",
        len(refreshed), len(failed), skipped,
    )
    # The unconditional FINAL-WARNING backstop (see gmp_final_expiry_warnings):
    # rides the same hourly tick, catches sessions the refresh loop can't see.
    try:
        gmp_final_expiry_warnings()
    except Exception:
        logger.exception("gmp_final_expiry_warnings failed")
    # Co-op (SmartHub) sessions have NO expiry field at all — their death alert
    # is data-driven (see coop_session_death_warnings). Same tick, same dedupe.
    try:
        coop_session_death_warnings()
    except Exception:
        logger.exception("coop_session_death_warnings failed")
    return {"refreshed": refreshed, "failed": failed, "skipped": skipped}


# Final-warning window: alert when the authoritative GMP JWT is inside this many
# days of expiry. By this point the extension keep-alive (T-8d window) has had
# 5+ days of chances and never succeeded — no browser has been open, or the
# portal session itself lapsed. Bills stop the moment the JWT dies.
_GMP_FINAL_WARN_DAYS = 3
# Re-alert cadence while the token keeps ticking down (bounded spam: at most
# one email every this-many days until it dies or is rescued).
_GMP_FINAL_WARN_REALERT_DAYS = 3


def gmp_final_expiry_warnings(days_ahead: int = _GMP_FINAL_WARN_DAYS,
                              dry_run: bool = False) -> dict:
    """The EXPLICIT token-death alert: 'your GMP token expires in N days and no
    keep-alive has run.' Unlike the refresh loop above, this pass is UNCONDITIONAL —
    it looks at expires_at alone, so it also covers sessions with no refresh_token
    (invisible to the loop) and fires even when failure counting never crossed its
    threshold. A successful extension keep-alive pushes expires_at ~21 days out,
    which silently disarms this alert — so firing at T-3d genuinely means nothing
    has rescued the session for days. De-duped via InverterAlertState
    (incident_key 'gmp_token_final:<tenant>'), re-armed on rescue."""
    from .models import InverterAlertState, UtilitySession, Tenant
    from .notify import send_gmp_reauth_needed_email, send_internal_alert

    cutoff = datetime.utcnow() + timedelta(days=days_ahead)
    out = {"warned": [], "skipped_dedup": 0, "rescued_cleared": 0, "dry_run": dry_run}
    with SessionLocal() as db:
        # Authoritative (newest-captured) GMP session per tenant — same selection
        # rule the bill sweep uses, so we alert on the session that matters.
        newest = {}
        for tid, sid, exp in db.execute(
            select(UtilitySession.tenant_id, UtilitySession.id, UtilitySession.expires_at)
            .where(UtilitySession.provider == "gmp")
            .order_by(UtilitySession.tenant_id, UtilitySession.captured_at.desc(),
                      UtilitySession.id.desc())
        ).all():
            newest.setdefault(tid, (sid, exp))

        for tid, (sid, exp) in newest.items():
            key = f"gmp_token_final:{tid}"
            state = db.execute(select(InverterAlertState).where(
                InverterAlertState.tenant_id == tid,
                InverterAlertState.incident_key == key)).scalar_one_or_none()
            # Long-dead sessions (expired >24h ago — abandoned test tenants, ancient
            # logins) are a post-mortem, not a *final warning* — never alert on them.
            # (Prod dry-run found 7 such tenants at 0.0d that would have spammed.)
            if exp is not None and exp < datetime.utcnow() - timedelta(hours=24):
                continue
            if exp is None or exp > cutoff:
                # Healthy (keep-alive rescued it, or fresh login) → close any open
                # incident so the NEXT death is a fresh alert.
                if state is not None:
                    if not dry_run:
                        db.delete(state)
                        db.commit()
                    out["rescued_cleared"] += 1
                continue
            days_left = max(0.0, (exp - datetime.utcnow()).total_seconds() / 86400)
            if state is not None and state.last_alerted_at is not None and \
                    datetime.utcnow() - state.last_alerted_at < timedelta(days=_GMP_FINAL_WARN_REALERT_DAYS):
                out["skipped_dedup"] += 1
                continue
            tenant = db.get(Tenant, tid)
            if tenant is None:
                continue
            # A paused / inactive tenant (trial ended, no card, read-only) is not
            # running bills, so a dying GMP token is not actionable — skip it so an
            # abandoned account can't page the operator (seen: ten_3c50bd000f638ed4,
            # active=False, spammed hourly). Active operators still get warned.
            if not getattr(tenant, "active", True):
                out["skipped_dedup"] += 1
                continue
            out["warned"].append({"tenant": tid, "email": tenant.contact_email,
                                  "days_left": round(days_left, 1)})
            if dry_run:
                continue
            # Persist the dedup state FIRST so a send failure can't re-fire hourly.
            if state is None:
                state = InverterAlertState(tenant_id=tid, incident_key=key)
                db.add(state)
            state.last_alerted_at = datetime.utcnow()
            db.commit()
            try:
                send_gmp_reauth_needed_email(
                    to=tenant.contact_email,
                    name=tenant.operator_name or tenant.company_name or tenant.name,
                    product=tenant.product,
                )
            except Exception:
                logger.exception("final-warning email failed for %s", tid)
            try:
                send_internal_alert(
                    f"GMP token FINAL WARNING: {tid} dies in {days_left:.1f}d, no keep-alive has run",
                    f"Tenant: {tid} ({tenant.contact_email})\nSession: {sid}\n"
                    f"Expires: {exp}\nThe extension keep-alive window opened at T-8d and "
                    f"never succeeded — no browser has been open (or the portal session "
                    f"lapsed). Bills stop when this JWT dies. Operator emailed to re-login.",
                )
            except Exception:
                logger.exception("final-warning internal alert failed for %s", tid)
    logger.info("gmp_final_expiry_warnings: warned=%d dedup=%d rescued=%d dry=%s",
                len(out["warned"]), out["skipped_dedup"], out["rescued_cleared"], dry_run)
    return out


# Co-op staleness bar: SmartHub posts day-totals with ~1-2 days of lag and
# weekends happen, so "no new smarthub row for this many days" is the earliest
# HONEST death signal (a dead token can't be read from any expiry field — there
# isn't one). 4 days ≈ pull failing for 2+ consecutive nights past normal lag.
_COOP_STALE_DAYS = 4
_COOP_REALERT_DAYS = 3


def coop_session_death_warnings(days_stale: int = _COOP_STALE_DAYS,
                                dry_run: bool = False) -> dict:
    """Data-driven death alert for co-op (SmartHub) sessions — VEC/WEC/sh_*.

    These sessions have NO expires_at, so the detector is evidence-of-life:
    a tenant whose smarthub pipeline USED to produce DailyGeneration rows
    (source='smarthub'), whose newest row is now > days_stale old, and whose
    session capture is ALSO older than days_stale (a fresh capture means the
    extension just re-logged — tonight's pull will recover on its own).
    Never fires for tenants that never had smarthub data (that's onboarding,
    not death). De-duped via InverterAlertState 'coop_session_dead:<tenant>:
    <provider>'; incident clears itself when data flows again."""
    from .adapters.smarthub import PROVIDER_TO_UTILITY, is_smarthub_provider
    from .models import InverterAlertState, UtilitySession, Tenant, DailyGeneration, UtilityAccount
    from .notify import send_coop_reauth_needed_email, send_internal_alert

    # What counts as EVIDENCE the session is alive: rows the session-riding paths
    # write — 'utility_meter' (the extension's SmartHub usage capture, the ONLY
    # path that has ever produced co-op rows in prod) and 'smarthub' (the
    # server-side pull, included for when it starts working). 'bill_prorate' is
    # EXCLUDED on purpose: it's a bill-derived estimate whose dates advance with
    # billing cycles, not with session health — counting it would mask a death.
    _LIVE_SOURCES = ("utility_meter", "smarthub")

    out = {"warned": [], "skipped_dedup": 0, "recovered_cleared": 0, "dry_run": dry_run}
    now_ = datetime.utcnow()
    with SessionLocal() as db:
        # Tenant × co-op provider pairs that have a stored session.
        pairs = [(tid, prov) for tid, prov in db.execute(
            select(UtilitySession.tenant_id, UtilitySession.provider)
            .where(UtilitySession.provider != "gmp").distinct()).all()
            if is_smarthub_provider(prov)]
        for tid, prov in pairs:
            key = f"coop_session_dead:{tid}:{prov}"
            state = db.execute(select(InverterAlertState).where(
                InverterAlertState.tenant_id == tid,
                InverterAlertState.incident_key == key)).scalar_one_or_none()

            # Newest live-source row on the arrays LINKED to this provider's
            # accounts — provider-scoped so a healthy VEC can't mask a dead WEC.
            newest_day = db.execute(
                select(func.max(DailyGeneration.day))
                .join(UtilityAccount, UtilityAccount.array_id == DailyGeneration.array_id)
                .where(
                    UtilityAccount.tenant_id == tid,
                    UtilityAccount.provider == prov,
                    UtilityAccount.enabled.is_(True),
                    UtilityAccount.deleted_at.is_(None),
                    DailyGeneration.source.in_(_LIVE_SOURCES))).scalar()
            if newest_day is None:
                continue          # never produced data → onboarding, not a death
            fresh = (now_.date() - newest_day).days < days_stale
            if fresh:
                if state is not None:      # recovered → close the incident
                    if not dry_run:
                        db.delete(state)
                        db.commit()
                    out["recovered_cleared"] += 1
                continue

            # Stale — but a capture newer than the staleness bar means the
            # extension already rescued the session; tonight's pull recovers it.
            last_cap = db.execute(
                select(func.max(UtilitySession.captured_at)).where(
                    UtilitySession.tenant_id == tid,
                    UtilitySession.provider == prov)).scalar()
            if last_cap is not None and now_ - last_cap < timedelta(days=days_stale):
                continue

            # Alert once, then re-alert at most every _COOP_REALERT_DAYS while it
            # stays dead -- NOT a tight re-alert loop (that flooded the operator
            # ~100x, 2026-07-06), but never permanently silent either: a session
            # dead for weeks deserves more than the one email it got at hour zero
            # (Ford, 2026-07-08: "find every instance of us intentionally
            # sabotaging our own reliability"). Same bounded-recurring shape as
            # gmp_final_expiry_warnings above.
            if state is not None and state.last_alerted_at is not None and \
                    now_ - state.last_alerted_at < timedelta(days=_COOP_REALERT_DAYS):
                out["skipped_dedup"] += 1
                continue
            tenant = db.get(Tenant, tid)
            if tenant is None:
                continue
            # Inactive / paused tenants aren't invoicing off this feed, so a dead
            # co-op session isn't actionable — don't page on it.
            if not getattr(tenant, "active", True):
                out["skipped_dedup"] += 1
                continue
            info = PROVIDER_TO_UTILITY.get(prov) or {}
            util_name = info.get("name") or prov.upper()
            portal = "https://%s/" % info["host"] if info.get("host") else "https://smarthub.coop/"
            days_dark = (now_.date() - newest_day).days
            out["warned"].append({"tenant": tid, "provider": prov,
                                  "email": tenant.contact_email, "days_dark": days_dark})
            if dry_run:
                continue
            # Persist the dedup state FIRST and commit, so a failure in either send
            # below can never leave the incident un-stamped and re-fire next tick.
            if state is None:
                state = InverterAlertState(tenant_id=tid, incident_key=key)
                db.add(state)
            state.last_alerted_at = now_
            db.commit()
            try:
                send_coop_reauth_needed_email(
                    to=tenant.contact_email,
                    name=tenant.operator_name or tenant.company_name or tenant.name,
                    utility_name=util_name, portal_url=portal,
                    product=tenant.product,
                )
            except Exception:
                logger.exception("co-op reauth email failed for %s/%s", tid, prov)
            try:
                send_internal_alert(
                    f"Co-op session DEAD: {tid} {prov} — no generation for {days_dark}d",
                    f"Tenant: {tid} ({tenant.contact_email})\nProvider: {prov} ({util_name})\n"
                    f"Newest smarthub generation day: {newest_day} ({days_dark}d ago)\n"
                    f"Last session capture: {last_cap}\nServer-side pulls are failing "
                    f"silently — no expiry field exists for co-op tokens, so this "
                    f"data-staleness alert is the death signal. Operator emailed.",
                )
            except Exception:
                logger.exception("co-op internal alert failed for %s/%s", tid, prov)
    logger.info("coop_session_death_warnings: warned=%d dedup=%d recovered=%d dry=%s",
                len(out["warned"]), out["skipped_dedup"], out["recovered_cleared"], dry_run)
    return out


def hard_delete_old_soft_deleted():
    """Purge rows whose deleted_at is older than 30 days.

    Order: utility_accounts → arrays → clients (FK-safe).
    Expired delete_history rows are also pruned here."""
    cutoff = datetime.utcnow() - timedelta(days=30)
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM utility_accounts WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff"
        ), {"cutoff": cutoff})
        conn.execute(text(
            "DELETE FROM arrays WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff"
        ), {"cutoff": cutoff})
        conn.execute(text(
            "DELETE FROM clients WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff"
        ), {"cutoff": cutoff})
        conn.execute(text(
            "DELETE FROM delete_history WHERE expires_at < :cutoff"
        ), {"cutoff": cutoff})


def poll_all_sources_job() -> dict:
    """Scheduler wrapper for the data-hub poller. Catches/logs so a poll error
    never crashes the scheduler thread."""
    from .poller import poll_all_sources
    try:
        summary = poll_all_sources()
        if summary.get("readings_written"):
            logger.info("poller: %s arrays, %s readings written",
                        summary["arrays_polled"], summary["readings_written"])
        if summary.get("errors"):
            logger.warning("poller: %d source errors this tick", len(summary["errors"]))
        return summary
    except Exception as e:
        logger.exception("poller: poll_all_sources crashed")
        # This is the data-hub spine (poller.py's own term) -- a persistent crash here
        # silently stops ALL live kW updates fleet-wide. Every sibling job wrapper in
        # this file alerts on crash; this one didn't (Ford, 2026-07-08: "find every
        # instance of us intentionally sabotaging our own reliability").
        send_internal_alert("poll_all_sources crashed", f"Error: {e}")
        return {"ran": False, "error": "exception"}


def prune_inverter_readings_job() -> dict:
    from .poller import prune_old_readings
    try:
        return prune_old_readings()
    except Exception as e:
        logger.exception("poller: prune_old_readings crashed")
        send_internal_alert("prune_old_readings crashed", f"Error: {e}")
        return {"pruned": 0, "error": "exception"}


def prune_harvest_runs_job() -> dict:
    """Hard-delete old harvest_run audit rows (Cloud Capture vault hygiene)."""
    from .vault_retention import prune_harvest_runs
    try:
        return prune_harvest_runs()
    except Exception as e:
        logger.exception("vault: prune_harvest_runs crashed")
        send_internal_alert("prune_harvest_runs crashed", f"Error: {e}")
        return {"deleted": 0, "error": "exception"}


def _prewarm_reconcile() -> None:
    """Keep the bill-audit reconcile sweep cache HOT so the offtaker-invoicing
    "Doesn't match GMP" KPI + Bill-audit view load INSTANTLY instead of waiting
    ~9-11s on the N+1 compute (Ford 2026-07-07). `build_match` dominates that
    compute (~82%) AND is the math-critical cross-check, so we move the cost OFF the
    request path rather than change the math: recompute reconcile_tenant on an 8-min
    cadence (< the 10-min `_sweeps` TTL, so a warmed entry never goes stale mid-window)
    and store it into the SAME in-process cache /reconcile-bills reads. Warms only AO
    tenants that are actually active — already viewed this process-life, or with a
    draft created in the last 3 days (auto-draft runs on every load, so a recent draft
    == recently used). Bounded to 15 tenants/cycle; each compute owns its session."""
    try:
        import time as _time
        from .billing.routes import _sweeps, _sweeps_lock
        from .billing.reconcile_bills import reconcile_tenant
        from .models import ReportDraft
        tids: list[str] = []
        with _sweeps_lock:
            tids += [k[0] for k in _sweeps.keys() if k[1] == "reconcile"]
        cutoff = datetime.utcnow() - timedelta(days=3)
        with SessionLocal() as db:
            rows = db.execute(
                select(ReportDraft.tenant_id)
                .where(ReportDraft.created_at >= cutoff)
                .group_by(ReportDraft.tenant_id)
            ).all()
        tids += [r[0] for r in rows]
        seen: set[str] = set()
        warmed = 0
        for tid in tids:
            if tid in seen:
                continue
            seen.add(tid)
            if warmed >= 15:
                break
            try:
                with SessionLocal() as db2:   # each compute OWNS its session (pool-leak rule)
                    res = reconcile_tenant(db2, tid)
                with _sweeps_lock:
                    _sweeps[(tid, "reconcile")] = {"status": "done", "result": res, "at": _time.time()}
                warmed += 1
            except Exception:  # noqa: BLE001
                logger.warning("prewarm reconcile failed for %s", tid, exc_info=True)
        logger.info("prewarm_reconcile: warmed %d tenant sweep(s)", warmed)
    except Exception:  # noqa: BLE001
        logger.exception("prewarm_reconcile job crashed")


def refresh_fleet_forecasts() -> dict:
    """Precompute the /forecast-fleet payload for every fleeted tenant, off the
    request path, so the Analysis tab loads INSTANTLY (the endpoint serves the
    stored snapshot instead of geocoding + calling Open-Meteo per site). Cheap on
    repeat ticks: forecasting's own POA/sky caches mean re-computation mostly
    reuses the last fetch. One bad tenant never stops the rest."""
    from .array_owners import (compute_fleet_forecast, _store_fleet_snapshot,
                               _FLEET_SNAPSHOT_WINDOWS)
    from .models import Array
    from sqlalchemy import select
    built = 0
    try:
        with SessionLocal() as db:
            tenants = db.execute(select(Tenant).where(Tenant.active.is_(True))).scalars().all()
        for t in tenants:
            with SessionLocal() as db:
                has_array = db.execute(
                    select(Array.id).where(Array.tenant_id == t.id,
                                           Array.deleted_at.is_(None)).limit(1)
                ).first()
            if not has_array:
                continue
            for w in _FLEET_SNAPSHOT_WINDOWS:
                try:
                    payload = compute_fleet_forecast(t, w)
                    with SessionLocal() as db:
                        _store_fleet_snapshot(db, t.id, w, payload)
                    built += 1
                except Exception:
                    logger.warning("refresh_fleet_forecasts: tenant=%s window=%s failed",
                                   t.id, w, exc_info=True)
    except Exception:
        logger.exception("refresh_fleet_forecasts crashed")
    return {"snapshots_built": built}


def stop() -> None:
    """Idempotent scheduler shutdown for process exit / FastAPI lifespan.

    Registered via atexit from start() so we stop the job thread *before*
    concurrent.futures tears down the pool (LIFO atexit order).
    """
    if not getattr(scheduler, "running", False):
        return
    try:
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
    except Exception:
        logger.exception("scheduler stop failed")


def start():
    """Register all jobs and start BackgroundScheduler. Idempotent.

    Safe to call once per process. If already running, no-ops so a double
    import / re-entry cannot re-register or call scheduler.start() twice.
    """
    global _atexit_stop_registered
    if scheduler.running:
        logger.info("scheduler already running — skip re-register")
        return
    # Every 6 hours, enqueue pull-bills jobs for each active tenant
    scheduler.add_job(
        enqueue_pull_for_all_tenants,
        "interval", hours=6, id="enqueue_pull_bills", replace_existing=True,
    )
    # Every 8 min: keep the bill-audit reconcile sweep HOT for active AO tenants so
    # the "Doesn't match GMP" KPI + Bill audit load instantly (Ford 2026-07-07). 8 min
    # < the 10-min sweep TTL, so a warmed tenant never goes cold mid-window. First run
    # ~90s after startup so a fresh deploy warms quickly.
    scheduler.add_job(
        _prewarm_reconcile,
        "interval", minutes=8, id="prewarm_reconcile", replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=90),
    )
    # Hourly: finalize expired trials (charge or extend)
    scheduler.add_job(
        finalize_expired_trials,
        "interval", hours=1, id="finalize_expired_trials", replace_existing=True,
    )
    # Hourly: refresh GMP sessions expiring within 7 days
    scheduler.add_job(
        refresh_expiring_gmp_tokens,
        "interval", hours=1, id="refresh_gmp_tokens", replace_existing=True,
    )
    # Daily at 08:00 UTC: remind no-card trialing operators ~3 days out.
    scheduler.add_job(
        send_trial_ending_reminders,
        CronTrigger(hour=8, minute=0),
        id="trial_ending_reminders", replace_existing=True,
    )
    # Cloud Capture launch announcement — fires ONCE at/after
    # CLOUD_CAPTURE_ANNOUNCE_AT (KVFlag-guarded, self-no-ops until then). A 15-min
    # interval means it goes out within 15 min of the scheduled time.
    from .jobs.cloud_capture_announce import maybe_send_scheduled
    scheduler.add_job(
        maybe_send_scheduled,
        "interval", minutes=15, id="cloud_capture_announce", replace_existing=True,
    )
    # Energy Agent + redesign announcement — fires ONCE at/after
    # ENERGY_AGENT_ANNOUNCE_AT (same KVFlag fire-once pattern).
    from .jobs.energy_agent_announce import maybe_send_scheduled as maybe_send_ea_announce
    scheduler.add_job(
        maybe_send_ea_announce,
        "interval", minutes=15, id="energy_agent_announce", replace_existing=True,
    )
    # Drain the queue every minute
    from .worker import run_pending_jobs
    scheduler.add_job(
        run_pending_jobs, "interval", minutes=1, id="run_pending_jobs", replace_existing=True,
    )
    # Every 15 min: watch Array Operator fleets — auto-open warranty claims for
    # newly failed inverters, close recovered ones, fire due grace-timer sends.
    scheduler.add_job(
        reconcile_warranty_claims,
        "interval", minutes=15, id="reconcile_warranty_claims", replace_existing=True,
    )
    # Every 15 min: O&M healing — open repair tickets for dead/fault with a
    # known service contact, clear recovered, fire due tech check-ins.
    scheduler.add_job(
        reconcile_repair_ops,
        "interval", minutes=15, id="reconcile_repair_ops", replace_existing=True,
    )
    # Every 10 min: Energy Agent reminders/watches the owner asked her to keep
    # (time reminders + condition watches like "tell me if an inverter goes down")
    # → email + chat when they fire. Distinct from the robotic Fleet Alerts.
    scheduler.add_job(
        _run_ea_reminders,
        IntervalTrigger(minutes=10), id="ea_reminders", replace_existing=True,
    )
    # RE-ENABLED 2026-07-08 (was briefly paused same day: "too many real accounts
    # stale" turned out to be Ford's own test tenants, since is_demo was never
    # filtered here -- see fronius-real-stale-census memory. Now fixed at the
    # root: vip_watch_sweep filters is_demo AND uses one universal fast bar, no
    # "protect the inbox" tier -- pausing this permanently would itself be the
    # exact reliability-sabotage Ford flagged same day).
    from .vip_watch import vip_watch_sweep
    scheduler.add_job(
        vip_watch_sweep,
        "interval", minutes=10, id="vip_watch_sweep", replace_existing=True,
    )
    # DATA HUB: every 5 min, poll every array with a pullable vendor connection
    # (SolarEdge live today; SMA/Fronius/etc. as their API creds come online) and
    # write the time-series. Daylight-gated inside poll_all_sources (no night API
    # spend). This is what keeps "current kW" tracking the vendor continuously.
    scheduler.add_job(
        poll_all_sources_job,
        "interval", minutes=5, id="poll_inverter_sources", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    # Daily at 04:10 UTC: prune the high-frequency readings beyond the rolling
    # window (daily kWh is rolled into InverterDaily separately).
    scheduler.add_job(
        prune_inverter_readings_job,
        CronTrigger(hour=4, minute=10),
        id="prune_inverter_readings", replace_existing=True,
    )
    # Daily at 04:25 UTC: prune Cloud Capture harvest_run audit history
    # (default 45 days). Append-only table was unbounded with real customers.
    scheduler.add_job(
        prune_harvest_runs_job,
        CronTrigger(hour=4, minute=25),
        id="prune_harvest_runs", replace_existing=True,
    )
    # Every 30 min: precompute the fleet forecast per tenant so the Analysis tab
    # serves an instant snapshot instead of computing (geocode + Open-Meteo) on the
    # request path. coalesce + single instance so a slow tick never stacks up.
    scheduler.add_job(
        refresh_fleet_forecasts,
        "interval", minutes=30, id="refresh_fleet_forecasts", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    # Weekly: Mondays at 09:00 UTC
    scheduler.add_job(
        deliver_weekly_reports,
        CronTrigger(day_of_week="mon", hour=9, minute=0),
        id="deliver_weekly", replace_existing=True,
    )
    # Weekly Mondays 12:00 UTC (morning ET): FRESHNESS SCORECARD to the internal
    # alert address — the measured "was the data there when the product needed
    # it" number (7-day DailyGeneration coverage + live-source freshness +
    # utility-login health + digest holds). Ford 2026-07-04: replace the vibe
    # with a number. Pure read; see api/jobs/freshness_scorecard.py.
    scheduler.add_job(
        _run_freshness_scorecard,
        CronTrigger(day_of_week="mon", hour=12, minute=0),
        id="freshness_scorecard", replace_existing=True,
    )
    # Weekly settlement-audit digest DECOMMISSIONED 2026-06-21 (Ford: "audit is
    # dead"): the Array Operator Audit tab was removed (the #audit route is gone, and
    # audit.js/audit.css were dropped from the SPA), so this email linked owners to a
    # dead route. The job is no longer scheduled; deliver_weekly_audit_digest stays in
    # this module for reference / future revival.
    # Monthly: 1st of every month at 09:00 UTC
    scheduler.add_job(
        deliver_monthly_reports,
        CronTrigger(day=1, hour=9, minute=0),
        id="deliver_monthly", replace_existing=True,
    )
    # Quarterly: 1st of Jan/Apr/Jul/Oct at 09:00 UTC
    scheduler.add_job(
        deliver_quarterly_reports,
        CronTrigger(month="1,4,7,10", day=1, hour=9, minute=0),
        id="deliver_quarterly", replace_existing=True,
    )
    # Daily 11:00 UTC: post-send DELIVERY RECEIPT to each NEPOOL operator — what
    # went out to whom + Resend-confirmed delivered/bounced, ~2h after the 09:00
    # sends so the delivery webhooks have landed. Data-driven off ReportDelivery,
    # so a missed run self-heals on the next tick.
    scheduler.add_job(
        _run_delivery_receipts,
        CronTrigger(hour=11, minute=0),
        id="report_delivery_receipts", replace_existing=True,
    )
    # Daily 14:00 UTC: 2-DAY-AHEAD REVIEW digest — when a weekly/monthly/quarterly
    # batch is exactly 2 days out, email each NEPOOL operator what will be sent to
    # whom (recipient + data-ready/skip + last delivery health) so they can review
    # before it goes.
    scheduler.add_job(
        _run_presend_reviews,
        CronTrigger(hour=14, minute=0),
        id="report_presend_reviews", replace_existing=True,
    )
    # Daily 13:00 UTC: "come review your next bill" — when a NEW GMP bill has
    # landed for an offtaker (a newer bill period than we last prompted on), draft
    # their invoice and email the OPERATOR a "ready to review" prompt to the
    # Reports page. Deduped per bill period (review_emailed_period), so each new
    # GMP update fires exactly one prompt. Decoupled from the cadence run above so
    # the operator hears the moment the bill is captured, not on the 1st.
    scheduler.add_job(
        _run_new_bill_reviews,
        CronTrigger(hour=13, minute=0),
        id="new_bill_review_prompts", replace_existing=True,
    )
    # Array Operator automatic billing reports (invoice + summary), on the
    # cadence each subscription chose. 1st of month / 1st of quarter / Sept 1
    # for annual true-ups — all 09:00 UTC, matching the NEPOOL cadence above.
    scheduler.add_job(
        deliver_monthly_billing_reports,
        CronTrigger(day=1, hour=9, minute=0),
        id="deliver_billing_monthly", replace_existing=True,
    )
    scheduler.add_job(
        deliver_quarterly_billing_reports,
        CronTrigger(month="1,4,7,10", day=1, hour=9, minute=0),
        id="deliver_billing_quarterly", replace_existing=True,
    )
    scheduler.add_job(
        deliver_annual_billing_trueups,
        CronTrigger(month="9", day=1, hour=9, minute=0),
        id="deliver_billing_trueup", replace_existing=True,
    )
    # Daily at 03:00 UTC: hard-delete rows soft-deleted > 30 days ago
    scheduler.add_job(
        hard_delete_old_soft_deleted,
        CronTrigger(hour=3, minute=0),
        id="hard_delete_old", replace_existing=True,
    )
    # Daily at 03:15 UTC: synthetic GMP health check
    scheduler.add_job(
        _run_synthetic_gmp_monitor,
        CronTrigger(hour=3, minute=15),
        id="synthetic_gmp_monitor", replace_existing=True,
    )
    # Daily at 03:00 UTC: pull daily generation for ALL inverter connections
    # (every vendor), iterating InverterConnection rows + legacy solaredge arrays.
    # Rate-limit: 300 req/day per SolarEdge token; N arrays = N requests, well
    # inside. Errors per connection are logged but don't crash the scheduler.
    scheduler.add_job(
        _run_inverter_pull,
        CronTrigger(hour=3, minute=0),
        id="inverter_daily_pull", replace_existing=True,
    )
    # NOTE — there is deliberately NO server-side SmartHub pull job. The v1.9.25
    # design (jobs/smarthub_pull.py riding a stored authorizationToken) was proven
    # wrong on a live VEC HAR: SmartHub's usage API authenticates with the owner's
    # httpOnly session COOKIE, which the backend can never replay. The job ran
    # nightly for weeks and produced ZERO rows ever (audited 2026-07-02). Co-op
    # daily generation arrives via the extension's CLIENT-side pull
    # (smarthub_content.js v1.9.26 → utility-meter-capture → source='utility_meter'),
    # nudged by the capture-debt system when stale, and its death is alarmed by
    # coop_session_death_warnings. Do not re-add this job.
    # Daily at 04:15 UTC: SELF-HEALING deep-history backfill. The 03:00 pull only
    # reaches ~90 days, so a newly-connected inverter shows just the current year
    # in Trends. This heals any connection whose full multi-year history hasn't
    # been pulled yet (history_backfilled_at IS NULL) — stamps only on success so
    # a failed/partial attempt retries next run. Capped per-run to respect vendor
    # rate limits; the connect endpoint also fires it immediately for new connects.
    scheduler.add_job(
        _run_history_heal,
        CronTrigger(hour=4, minute=15),
        id="inverter_history_heal", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    # Daily at 04:45 UTC: auto-dedup duplicate arrays. The GMP absorption feature
    # can create a GMP twin of a vendor array; this merges the unambiguous ones
    # (shared utility account / identical name / cross-source name containment)
    # and leaves anything questionable as a one-click suggestion. LOSSLESS +
    # undo-logged. Runs after the history heal so merged arrays carry full data.
    scheduler.add_job(
        _run_array_dedup,
        CronTrigger(hour=4, minute=45),
        id="array_dedup_sweep", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    # Daily at 03:30 UTC: snapshot per-inverter daily history into InverterDaily for
    # every owner (persist-on-read forced on a schedule) so the per-inverter graphs
    # keep accumulating real history even when nobody opens the dashboard. Critical
    # for SolarEdge, whose per-inverter telemetry is otherwise live-API-only.
    scheduler.add_job(
        _run_inverter_history_snapshot,
        CronTrigger(hour=3, minute=30),
        id="inverter_history_snapshot", replace_existing=True,
    )
    # Daily at 03:45 UTC: billing-safety watchdog — alert if any physically
    # impossible kWh row exists in DailyGeneration (billing meter) or
    # InverterDaily. Runs AFTER the 03:30 snapshot and BEFORE the 04:00 usage
    # report, so a bad row is caught and alerted before it can bill.
    scheduler.add_job(
        _run_generation_watchdog,
        CronTrigger(hour=3, minute=45),
        id="generation_watchdog", replace_existing=True,
    )
    # Daily (13:30 UTC): GMP data-freshness watchdog — alert if any active GMP
    # tenant has stopped capturing (extension dead / GMP login expired), so we never
    # silently bill or report from frozen data. Runs DAILY now (was weekly, which let
    # a frozen capture go unseen up to ~14 days on the billing-critical path); the
    # watchdog's per-tenant InverterAlertState dedup + _REALERT_DAYS bar prevent
    # alert fatigue without slowing detection (Ford, 2026-07-09: no scarcity-driven
    # self-sabotage).
    scheduler.add_job(
        _run_gmp_freshness_watchdog,
        CronTrigger(hour=13, minute=30),
        id="gmp_freshness_watchdog", replace_existing=True,
    )
    # Hourly: Cloud Capture lockout watchdog — alert while any stored portal login
    # is held at the lockout pause. The pause is the one legitimate back-off we
    # keep (a bad password hammered on a 3-min loop is how a portal locks the
    # account), but it used to be SILENT and permanent: a paused login just stopped
    # harvesting forever and only the owner re-saving the password brought it back.
    # It now retries on a slow heartbeat (scheduler.PAUSED_RETRY) and this watchdog
    # keeps the operator loudly informed until it recovers. Deliberately registered
    # here — on the scheduler-owning service, alongside every sibling watchdog —
    # and NOT inside the harvester's own loop, so a wedged harvester can't take its
    # own alarm down with it.
    # It also runs the STALL watchdog, which is cause-blind: the pause only counts
    # failures that spent a real password attempt, so everything else that kills a
    # capture would otherwise be silent — and `vip_watch`, the other staleness net,
    # was switched off 2026-07-19.
    # next_run_time matters: a bare IntervalTrigger(hours=1) first fires a full
    # hour after process start, so on a repo that auto-deploys several times an
    # hour this alarm would statistically almost never run. Fire shortly after
    # every boot instead; the InverterAlertState dedup makes extra runs free.
    scheduler.add_job(
        _run_cloud_capture_lockout_watchdog,
        IntervalTrigger(hours=1),
        id="cloud_capture_lockout_watchdog", replace_existing=True,
        next_run_time=datetime.utcnow() + timedelta(minutes=3),
    )
    # Every 2 min: drain automatic bill-adapter discovery jobs (unknown utility
    # logins). Bounded browser explore + synthesis; known families short-circuit.
    scheduler.add_job(
        _run_bill_discovery_drain,
        IntervalTrigger(minutes=2),
        id="bill_discovery_drain", replace_existing=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=45),
    )
    # Daily at 04:00 UTC: report Array Operator per-kWh usage to Stripe (LEGACY
    # metered billing). Self-skips subs with no metered line (i.e. nameplate subs),
    # so it's a harmless no-op once a tenant is migrated to per-kW nameplate.
    scheduler.add_job(
        _run_usage_report,
        CronTrigger(hour=4, minute=0),
        id="ao_usage_report", replace_existing=True,
    )
    # Daily at 04:05 UTC: sync Array Operator per-kW NAMEPLATE billing quantity to
    # each sub's registered nameplate (kW). The authoritative quantity mechanism +
    # safety net for the nameplate billing model.
    scheduler.add_job(
        _run_nameplate_sync,
        CronTrigger(hour=4, minute=5),
        id="ao_nameplate_sync", replace_existing=True,
    )
    # Hourly: inverter down/underperformance email-alert sweep (Array Operator).
    # Safe to run frequently — the per-incident grace window + de-dup state
    # (InverterAlertState) ensure one email per incident, not one per tick.
    # Hourly keeps incident detection responsive without spamming owners.
    scheduler.add_job(
        _run_inverter_alert_sweep,
        CronTrigger(minute=20),
        id="inverter_alert_sweep", replace_existing=True,
    )
    # Daily at 12:00 UTC (~7-8am ET): morning fleet-health digest. One
    # tenant-facing email per active Array Operator owner with KPIs, highlights,
    # and per-array health rendered from build_fleet_tree. Read-only on data.
    scheduler.add_job(
        _run_morning_fleet_digest,
        CronTrigger(hour=12, minute=0),
        id="morning_fleet_digest", replace_existing=True,
        # Explicit (also covered by job_defaults): a restart anywhere in the noon
        # hour must not silently drop that day's digest. coalesce so a backlog
        # never sends twice.
        misfire_grace_time=3600, coalesce=True,
    )
    # 1st of each month 13:00 UTC: Performance Verification monthly report pack
    # (prior calendar month PI/PR PDF + email) for AO tenants with
    # verification_reports_enabled. See api/jobs/verification_monthly.py.
    scheduler.add_job(
        _run_verification_monthly_reports,
        CronTrigger(day=1, hour=13, minute=0),
        id="verification_monthly_reports", replace_existing=True,
        misfire_grace_time=3600, coalesce=True,
    )
    # Daily at 15:00 UTC: watchdog. If the noon digest did NOT run today (missed
    # trigger, crash before the heartbeat), turn that invisible miss into a visible
    # internal alert instead of silence. Reads the KVFlag heartbeat below.
    scheduler.add_job(
        _run_morning_digest_watchdog,
        CronTrigger(hour=15, minute=0),
        id="morning_digest_watchdog", replace_existing=True,
        misfire_grace_time=3600, coalesce=True,
    )
    # Every 15 min: inbound-email safety net. Re-lists Resend receiving and
    # ingests anything the live webhook missed (deduped on the Resend id) —
    # covers BOTH the repair-tech loop and the owner agent mailbox, so one
    # dropped webhook never silently strands a reply.
    scheduler.add_job(
        _run_inbound_email_sync,
        IntervalTrigger(minutes=15),
        id="inbound_email_sync", replace_existing=True,
    )
    # Mondays 13:00 UTC (~9am ET): the AGENTIC weekly check-in — Energy Agent
    # writes each owner a first-person note (what I handled / what I noticed /
    # reply and I act) composed from live fleet+repairs+invoice data. Opt-out
    # per tenant via the signed footer link. See energy_agent_email.py.
    scheduler.add_job(
        _run_ea_weekly_checkin,
        CronTrigger(day_of_week="mon", hour=13, minute=0),
        id="ea_weekly_checkin", replace_existing=True,
    )
    # Daily at 05:00 UTC: GMP daily-generation sponge top-up. Walks each GMP
    # meter's full multi-year history on first run, then incrementally tops up.
    # Feeds Trends + Reports with GMP data hands-free. Runs after the 03:xx
    # inverter pulls and before the 09:00 report deliveries.
    scheduler.add_job(
        _run_gmp_daily_backfill,
        CronTrigger(hour=5, minute=0),
        id="gmp_daily_backfill", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    # Daily at 05:30 UTC: Bill→daily transform. Converts captured GMP bills'
    # generation into bill_prorate DailyGeneration rows so parsed bills SHOW in
    # Trends + merge with inverter data. Runs AFTER the 05:00 GMP daily-interval
    # backfill so granular GMP-API days land first; bill-prorate only fills the
    # remaining gaps and never overwrites a real metered reading.
    scheduler.add_job(
        _run_bill_to_daily,
        CronTrigger(hour=5, minute=30),
        id="bill_to_daily", replace_existing=True,
        max_instances=1, coalesce=True,
    )
    # Every 60s: watch the SQLAlchemy pool. If it stays near-exhausted across
    # two consecutive ticks, dispose + alert so the process self-heals instead
    # of wedging forever (2026-07-14 QueuePool outage). Async-safe counters only.
    scheduler.add_job(
        _run_pool_watchdog,
        "interval", seconds=60, id="db_pool_watchdog", replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=45),
    )
    # Energy Agent operating mind: drain cheap background tasks for tenants with
    # recent open sessions (continuous awareness without per-second cost).
    scheduler.add_job(
        _run_energy_agent_mind_tick,
        "interval", seconds=90, id="energy_agent_mind_tick", replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=60),
    )
    # Long-term mind: proactive insights + world model even when no chat open.
    scheduler.add_job(
        _run_energy_agent_long_term_mind,
        "interval", minutes=20, id="energy_agent_long_term_mind", replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(minutes=3),
    )
    # Sovereign desk brain drain (web offloads turns → worker finishes them).
    # Independent of SOVEREIGN_ENABLED so Ford's desk works with mind heavy work
    # paused. Kill: SOVEREIGN_DESK_ENABLED=0 or SOVEREIGN_DESK_DRAIN=0.
    scheduler.add_job(
        _run_energy_agent_sovereign_desk_drain,
        "interval", seconds=12, id="energy_agent_sovereign_desk_drain",
        replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=8),
    )
    # Sovereign Mind (product executive) — three layers:
    #   subconscious (cheap, ~45s) → cortex (expensive, 5m backstop + event wakes)
    #   event bus: wake_sovereign from product touchpoints
    # Kill with SOVEREIGN_ENABLED=0. See docs/plans/2026-07-15-energy-agent-sovereign-mind.md
    scheduler.add_job(
        _run_energy_agent_sovereign_subconscious,
        "interval", seconds=45, id="energy_agent_sovereign_subconscious",
        replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=40),
    )
    scheduler.add_job(
        _run_energy_agent_sovereign_tick,
        "interval", minutes=5, id="energy_agent_sovereign_tick", replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(minutes=2),
    )
    # Sovereign expansion mission loop (outside sub/cortex cadence).
    # HAR recon, cred live refresh, light job drain. Kill: SOVEREIGN_EXPAND=0
    scheduler.add_job(
        _run_energy_agent_sovereign_mission_loop,
        "interval", minutes=2, id="energy_agent_sovereign_mission_loop",
        replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(minutes=3),
    )
    # Sovereign code worker: Claude Code / Grok implement queued jobs + push/deploy.
    # Ford authorized live ship 2026-07-15. Kill: SOVEREIGN_CODE_LIVE=0
    scheduler.add_job(
        _run_energy_agent_sovereign_jobs,
        "interval", minutes=3, id="energy_agent_sovereign_jobs", replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(minutes=4),
    )
    # Sovereign durability: independent watchdog (interface dual-sidecar pattern).
    # Diagnoses stale sub/cortex/jobs; soft-reboots recovery channel; storm breaker.
    # Kill: SOVEREIGN_WATCHDOG=0
    scheduler.add_job(
        _run_energy_agent_sovereign_watchdog,
        "interval", seconds=75, id="energy_agent_sovereign_watchdog",
        replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=95),
    )
    # Sovereign skill evolution (Hermes closed learning loop).
    # Harvest job/ops traces → create/patch SKILL.md playbooks → curator.
    # Kill: SOVEREIGN_SKILLS=0  /  SOVEREIGN_SKILL_EVOLVE=0
    scheduler.add_job(
        _run_energy_agent_sovereign_skills,
        "interval", minutes=20, id="energy_agent_sovereign_skills",
        replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(minutes=5),
    )
    # Weekly async check-in digest to Ford (high-level, no job spam).
    # Monday 14:00 UTC ≈ morning US East — review when convenient.
    scheduler.add_job(
        _run_energy_agent_sovereign_weekly_digest,
        CronTrigger(day_of_week="mon", hour=14, minute=0),
        id="energy_agent_sovereign_weekly_digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Ford Operator: standing Grok triage for Energy Agent escalations inbox.
    # Every 2 min process a small batch of open items → needs_ford + notify.
    scheduler.add_job(
        _run_ford_escalation_worker,
        "interval", minutes=2, id="ford_escalation_worker", replace_existing=True,
        max_instances=1, coalesce=True,
        next_run_time=datetime.utcnow() + timedelta(seconds=90),
    )
    scheduler.start()
    # atexit is LIFO: registered after concurrent.futures import, so stop()
    # runs before ThreadPoolExecutor's _python_exit and avoids the submit race.
    if not _atexit_stop_registered:
        atexit.register(stop)
        _atexit_stop_registered = True


def _run_ford_escalation_worker() -> None:
    """Triage open EA escalations with Grok; board at /admin/escalations."""
    try:
        from .ford_escalations import process_open_escalations
        res = process_open_escalations()
        if res.get("processed"):
            logger.info(
                "ford_escalation_worker: processed=%s notified=%s errors=%s",
                res.get("processed"), res.get("notified"), res.get("errors"),
            )
    except Exception:
        logger.exception("ford_escalation_worker crashed")


def _run_energy_agent_mind_tick() -> None:
    """Cognitive tick: observe → reprioritize → drain for tenants with open EA sessions."""
    try:
        from datetime import datetime, timedelta
        from sqlalchemy import select, distinct
        from .db import SessionLocal
        from .energy_agent import EaSession
        from .energy_agent_mind import mind_tick, sync_improvement_wins
        cutoff = datetime.utcnow() - timedelta(hours=24)
        with SessionLocal() as db:
            tenant_ids = db.execute(
                select(distinct(EaSession.tenant_id)).where(
                    EaSession.status == "open",
                    EaSession.created_at >= cutoff,
                )
            ).scalars().all()
        # One SHORT session per tenant — mind workers can hit LLM/Resend HTTP,
        # and a single session held across the whole fleet loop is the
        # documented pool-exhaustion meltdown class.
        for tid in tenant_ids[:40]:
            try:
                with SessionLocal() as db:
                    mind_tick(db, tid)
                    # Phase D: fold shipped feature suggestions into win events
                    sync_improvement_wins(db, tid)
                    db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("mind tick tenant %s: %s", tid, exc)
    except Exception as exc:  # noqa: BLE001
        try:
            send_internal_alert(
                "Energy Agent mind tick failed",
                f"Background cognition drain raised:\n{exc}",
            )
        except Exception:
            pass


def _run_energy_agent_long_term_mind() -> None:
    """Long-term mind: wake tenants with a world model or recent AO activity.

    Feels like one mind that keeps caring about the account between visits —
    fleet pulse + proactive insight, email when something worth knowing.
    """
    try:
        from datetime import datetime, timedelta
        from sqlalchemy import select, distinct
        from .db import SessionLocal
        from .energy_agent import EaSession
        from .energy_agent_mind import (
            EaWorldState, wake_mind, mind_tick, sync_improvement_wins,
        )
        from .models import Tenant

        cutoff = datetime.utcnow() - timedelta(days=14)
        with SessionLocal() as db:
            # Tenants that already have a mind (world row) OR recent EA session
            world_ids = set(
                db.execute(select(EaWorldState.tenant_id)).scalars().all()
            )
            session_ids = set(
                db.execute(
                    select(distinct(EaSession.tenant_id)).where(
                        EaSession.created_at >= cutoff,
                    )
                ).scalars().all()
            )
            # Prefer array_operator product for proactive fleet UX
            ao_ids = set(
                db.execute(
                    select(Tenant.id).where(
                        Tenant.product == "array_operator",
                        Tenant.active.is_(True),
                        Tenant.is_demo.is_(False),
                    ).limit(80)
                ).scalars().all()
            )
            known = world_ids | session_ids
            # Only tenants we already know (world or session) — avoid cold spam
            if ao_ids:
                candidates = list(known & ao_ids)
            else:
                candidates = list(known)
        # One SHORT session per tenant (LLM/Resend HTTP runs inside mind work —
        # never hold a session across the whole fleet loop).
        for tid in candidates[:25]:
            try:
                with SessionLocal() as db:
                    wake_mind(db, tid, "scheduled_proactive", enqueue_insight=True)
                    mind_tick(db, tid)
                    sync_improvement_wins(db, tid)
                    # Standing objective: nudge the single highest-value setup/
                    # operational gap (silent when fully operational).
                    try:
                        from .energy_agent_mind import evaluate_and_nudge_gap
                        evaluate_and_nudge_gap(db, tid)
                    except Exception as gexc:  # noqa: BLE001
                        logger.info("gap nudge %s: %s", tid, gexc)
                    db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("long-term mind tenant %s: %s", tid, exc)
    except Exception as exc:  # noqa: BLE001
        try:
            send_internal_alert(
                "Energy Agent long-term mind failed",
                f"Proactive cognition raised:\n{exc}",
            )
        except Exception:
            pass


def _sov_guard_skip(layer: str) -> bool:
    """Central heavy-work gate (pool pressure / pause / enabled). See sovereign_guard."""
    try:
        from .sovereign_guard import should_skip_heavy
        skip, reason = should_skip_heavy(layer)
        if skip:
            logger.warning("sovereign_%s skipped: %s", layer, reason)
        return skip
    except Exception as exc:  # noqa: BLE001
        # Fail open only if guard itself is broken — prefer keeping API alive
        # by treating unknown as skip when pool looks hot.
        logger.exception("sovereign_guard failed (%s): %s", layer, exc)
        try:
            from .sovereign_guard import pool_too_hot
            if pool_too_hot():
                logger.warning("sovereign_%s skipped: guard_error+pool_hot", layer)
                return True
        except Exception:
            pass
        return False


def _sov_heavy_begin(layer: str) -> tuple[bool, str]:
    """Acquire process-local heavy single-flight. On failure, log + return (False, reason)."""
    try:
        from .sovereign_guard import try_begin_heavy
        ok, reason = try_begin_heavy(layer)
        if not ok:
            logger.warning("sovereign_%s skipped: %s", layer, reason)
        return ok, reason
    except Exception as exc:  # noqa: BLE001
        logger.exception("sovereign single_flight begin failed (%s): %s", layer, exc)
        # Fail open on lock machinery errors so work still runs
        return True, "ok"


def _sov_heavy_end(layer: str) -> None:
    try:
        from .sovereign_guard import end_heavy
        end_heavy(layer)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sovereign single_flight end failed (%s): %s", layer, exc)


def _sov_pool_too_hot() -> bool:
    """Back-compat wrapper — prefer _sov_guard_skip / sovereign_guard."""
    try:
        from .sovereign_guard import pool_too_hot
        return pool_too_hot()
    except Exception:
        return False


def _run_energy_agent_sovereign_desk_drain() -> None:
    """Finish Ford desk turns enqueued by the web process (no LLM on web).

    Always-on while desk is enabled — does not require SOVEREIGN_ENABLED so
    the mind can stay paused without bricking Sovereign Desk chat.
    Skips when the worker DB pool is hot to protect shared Postgres / AO API.
    """
    try:
        from .energy_agent_sovereign_desk import (
            _flag as _desk_flag,
            drain_pending_desk_turns,
        )
        if not _desk_flag("SOVEREIGN_DESK_ENABLED", "1"):
            return
        if not _desk_flag("SOVEREIGN_DESK_DRAIN", "1"):
            return
        # Soft pool gate only — don't take the heavy single-flight lock so
        # cortex/jobs can still progress independently of desk chat.
        try:
            from .sovereign_guard import pool_too_hot
            if pool_too_hot():
                logger.warning("sovereign_desk_drain skipped: pool_hot")
                return
        except Exception:
            pass
        res = drain_pending_desk_turns()
        if res.get("recovered"):
            logger.info(
                "sovereign_desk_drain recovered=%s scanned=%s",
                res.get("recovered"), res.get("scanned"),
            )
    except Exception as exc:
        logger.exception("energy_agent_sovereign_desk_drain crashed: %s", exc)


def _run_energy_agent_sovereign_watchdog() -> None:
    """Independent durability supervisor for Sovereign (dual-channel recovery).

    Diagnose always runs (cheap). Heavy recovery (force sub/cortex/jobs) is
    gated inside watchdog via sovereign_guard so pool thrash cannot re-enter.
    """
    try:
        from .energy_agent_sovereign_watchdog import (
            persist_vitals_snapshot,
            watchdog_enabled,
            watchdog_tick,
        )
        if not watchdog_enabled():
            return
        res = watchdog_tick()
        if res.get("recovered") or res.get("mode") == "recovery":
            logger.warning(
                "sovereign_watchdog recovered mode=%s problems=%s",
                res.get("mode"),
                res.get("problems_seen") or (res.get("health") or {}).get("problems"),
            )
        elif res.get("mode") == "healthy":
            logger.debug("sovereign_watchdog healthy")
        elif res.get("skipped") or res.get("mode") == "guard_skip":
            logger.warning(
                "sovereign_watchdog heavy recovery skipped: %s",
                res.get("reason") or res.get("skip_reason"),
            )
        try:
            persist_vitals_snapshot()
        except Exception:
            pass
    except Exception as exc:
        logger.exception("energy_agent_sovereign_watchdog crashed: %s", exc)


def _run_energy_agent_sovereign_skills() -> None:
    """Hermes-style skill evolution: harvest traces → create/patch → curator."""
    try:
        from .energy_agent_sovereign_skills import evolution_cycle, skills_enabled
        if not skills_enabled():
            return
        if _sov_guard_skip("skills"):
            return
        ok, _reason = _sov_heavy_begin("skills")
        if not ok:
            return
        try:
            res = evolution_cycle()
            if res.get("created") or res.get("patched"):
                logger.info(
                    "sovereign_skills: created=%s patched=%s deprecated=%s traces=%s",
                    res.get("created"),
                    res.get("patched"),
                    (res.get("curator") or {}).get("deprecated"),
                    res.get("n_traces"),
                )
            elif not res.get("ok"):
                logger.warning("sovereign_skills cycle failed: %s", res.get("error"))
        finally:
            _sov_heavy_end("skills")
    except Exception as exc:
        logger.exception("energy_agent_sovereign_skills crashed: %s", exc)


def _run_energy_agent_sovereign_subconscious() -> None:
    """Cheap continuous mind: monologue + heat + needs_cortex (notes only)."""
    try:
        from .energy_agent_sovereign_subconscious import (
            subconscious_enabled,
            subconscious_tick,
            _run_cortex_if_needed,
        )
        from .energy_agent_sovereign_watchdog import mark_cortex_inflight, note_primary
        if not subconscious_enabled():
            return
        if _sov_guard_skip("subconscious"):
            return
        res = subconscious_tick(reason="scheduler")
        note_primary(
            "sub",
            ok=bool(res.get("ok")) and res.get("mode") not in ("error",),
            detail={"mode": res.get("mode"), "heat": res.get("heat"), "tick": res.get("tick_id")},
        )
        if res.get("needs_cortex"):
            # Cortex wake is heavy — re-check guard + single-flight before escalating.
            # Subconscious light tick already ran; skip wake only if busy/hot.
            if _sov_guard_skip("cortex_wake"):
                return
            ok, _reason = _sov_heavy_begin("cortex_wake")
            if not ok:
                return
            mark_cortex_inflight(True)
            try:
                cortex = _run_cortex_if_needed(res, reason="subconscious_hot")
            finally:
                mark_cortex_inflight(False)
                _sov_heavy_end("cortex_wake")
            if cortex and not cortex.get("deferred"):
                note_primary(
                    "cortex",
                    ok=bool(cortex.get("ok")),
                    detail={"via": "sub_wake", "tick": cortex.get("tick_id")},
                )
                logger.info(
                    "sovereign_subconscious→cortex heat=%s why=%s decisions=%s",
                    res.get("heat"),
                    res.get("why"),
                    len(cortex.get("decisions") or []),
                )
        elif res.get("mode") == "live":
            logger.debug(
                "sovereign_subconscious heat=%s needs=%s",
                res.get("heat"), res.get("needs_cortex"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("energy_agent_sovereign_subconscious crashed: %s", exc)
        try:
            from .energy_agent_sovereign_watchdog import note_primary
            note_primary("sub", ok=False, detail={"error": str(exc)[:200]})
        except Exception:
            pass


def _run_energy_agent_sovereign_mission_loop() -> None:
    """Long-running expand missions (HAR recon, cred refresh) every ~2m.

    mission_loop_tick owns short SessionLocal segments around browser HTTP —
    do not wrap it in an outer session (pool exhaustion class).
    # SESSION BOUNDARY: no LLM inside open session
    """
    try:
        from .energy_agent_sovereign import sovereign_enabled
        from .energy_agent_sovereign_expand import expand_enabled, mission_loop_tick
        if not sovereign_enabled() or not expand_enabled():
            return
        if _sov_guard_skip("mission_loop"):
            return
        ok, _reason = _sov_heavy_begin("mission_loop")
        if not ok:
            return
        try:
            res = mission_loop_tick()
            if res.get("steps"):
                logger.info("sovereign_mission_loop steps=%s", res.get("steps"))
        finally:
            _sov_heavy_end("mission_loop")
    except Exception as exc:  # noqa: BLE001
        logger.exception("energy_agent_sovereign_mission_loop crashed: %s", exc)


def _run_energy_agent_sovereign_tick() -> None:
    """Cortex backstop (5m): full digests + Grok/Claude + hard acts."""
    try:
        from .energy_agent_sovereign import sovereign_enabled, sovereign_tick
        from .energy_agent_sovereign_watchdog import mark_cortex_inflight, note_primary
        if not sovereign_enabled():
            return
        if _sov_guard_skip("cortex"):
            return
        ok, _reason = _sov_heavy_begin("cortex")
        if not ok:
            return
        try:
            # Light subconscious first so cortex always has fresh tape
            # (sub does not take heavy lock — already held by cortex)
            try:
                from .energy_agent_sovereign_subconscious import (
                    subconscious_enabled, subconscious_tick,
                )
                if subconscious_enabled():
                    sub = subconscious_tick(reason="pre_cortex", force=True, skip_llm=True)
                    note_primary(
                        "sub",
                        ok=bool(sub.get("ok")),
                        detail={"via": "pre_cortex", "tick": sub.get("tick_id")},
                    )
            except Exception as sub_exc:  # noqa: BLE001
                logger.debug("pre_cortex subconscious: %s", sub_exc)
                try:
                    note_primary("sub", ok=False, detail={"error": str(sub_exc)[:160], "via": "pre_cortex"})
                except Exception:
                    pass
            mark_cortex_inflight(True)
            try:
                res = sovereign_tick(reason="scheduler")
            finally:
                mark_cortex_inflight(False)
            note_primary(
                "cortex",
                ok=bool(res.get("ok")),
                detail={"tick": res.get("tick_id"), "mode": res.get("mode")},
            )
            if res.get("decisions"):
                logger.info(
                    "sovereign_cortex: decisions=%s heat=%s queues=%s",
                    len(res.get("decisions") or []),
                    res.get("heat"),
                    (res.get("digests") or {}).get("queues"),
                )
        finally:
            _sov_heavy_end("cortex")
    except Exception as exc:  # noqa: BLE001
        logger.exception("energy_agent_sovereign_tick crashed: %s", exc)
        try:
            from .energy_agent_sovereign_watchdog import mark_cortex_inflight, note_primary
            mark_cortex_inflight(False)
            note_primary("cortex", ok=False, detail={"error": str(exc)[:200]})
        except Exception:
            pass
        try:
            _sov_heavy_end("cortex")
        except Exception:
            pass
        try:
            send_internal_alert(
                "Energy Agent sovereign tick failed",
                f"Product mind raised:\n{exc}",
            )
        except Exception:
            pass


def _run_energy_agent_sovereign_jobs() -> None:
    """Drain Sovereign code-hire queue (Claude Code / Grok → push/deploy)."""
    try:
        from .db import SessionLocal
        from .energy_agent_sovereign_worker import code_live_enabled, drain_jobs
        from .energy_agent_sovereign_watchdog import mark_jobs_inflight, note_primary

        # Rocket thrust BEFORE pool/single-flight guards — only queues a DB job
        # (no Claude Code). Pool-hot was starving the engine entirely.
        try:
            with SessionLocal() as db:
                from .energy_agent_sovereign_rocket import maybe_thrust

                thrust = maybe_thrust(db)
                if thrust.get("thrust"):
                    logger.info("sovereign_rocket_thrust: %s", thrust)
                    db.commit()
                else:
                    db.rollback()
        except Exception as te:  # noqa: BLE001
            logger.warning("sovereign_rocket_thrust failed: %s", te)

        if not code_live_enabled():
            return
        if _sov_guard_skip("jobs"):
            return
        ok, _reason = _sov_heavy_begin("jobs")
        if not ok:
            return
        mark_jobs_inflight(True)
        try:
            with SessionLocal() as db:
                # Energy Agent Prime: one project at a time (default drain 1).
                # Cap 2 even if env is higher — never batch thrash the site.
                try:
                    from .energy_agent_prime_site import job_drain_limit
                    limit = job_drain_limit()
                except Exception:
                    try:
                        limit = int(os.getenv("SOVEREIGN_JOB_DRAIN_LIMIT", "1") or 1)
                    except (TypeError, ValueError):
                        limit = 1
                limit = min(max(1, limit), 2)
                res = drain_jobs(db, limit=limit)
                if res.get("processed"):
                    logger.info("sovereign_jobs: %s", res)
            note_primary(
                "jobs",
                ok=bool(res.get("ok", True)),
                detail={"processed": res.get("processed")},
            )
        finally:
            mark_jobs_inflight(False)
            _sov_heavy_end("jobs")
    except Exception as exc:  # noqa: BLE001
        logger.exception("energy_agent_sovereign_jobs crashed: %s", exc)
        try:
            from .energy_agent_sovereign_watchdog import mark_jobs_inflight, note_primary
            mark_jobs_inflight(False)
            note_primary("jobs", ok=False, detail={"error": str(exc)[:200]})
        except Exception:
            pass
        try:
            _sov_heavy_end("jobs")
        except Exception:
            pass
        try:
            send_internal_alert(
                "Sovereign code worker failed",
                f"Job drain raised:\n{exc}",
            )
        except Exception:
            pass


def _run_energy_agent_sovereign_weekly_digest() -> None:
    """Async weekly check-in: high-level digest email to Ford."""
    try:
        from .energy_agent_sovereign import sovereign_enabled, run_weekly_digest
        if not sovereign_enabled():
            return
        if _sov_guard_skip("weekly_digest"):
            return
        res = run_weekly_digest(force=False)
        if res.get("emailed"):
            logger.info("sovereign_weekly_digest: emailed ok")
        elif res.get("skipped"):
            logger.debug("sovereign_weekly_digest: skipped %s", res.get("reason"))
        else:
            logger.info("sovereign_weekly_digest: %s", res)
    except Exception as exc:  # noqa: BLE001
        logger.exception("sovereign_weekly_digest crashed: %s", exc)


def _run_bill_to_daily() -> None:
    """Prorate captured GMP bills into the daily-generation stream the frontend
    reads. Idempotent + incremental; real metered readings always win."""
    try:
        from .jobs.bill_to_daily import transform_all_tenants
        transform_all_tenants()
    except Exception as exc:  # noqa: BLE001
        send_internal_alert(
            "Bill→daily transform: unhandled exception",
            f"The bill-to-daily transformer raised an error:\n{exc}",
        )


# Consecutive high-pressure ticks before dispose (2 × 60s = ~2 min of saturation).
_pool_pressure_streak = 0


def _run_pool_watchdog() -> None:
    """Self-heal a wedged SQLAlchemy pool without a full Railway restart.

    If checked_out stays ≥ 85% of capacity for two consecutive ticks, dispose
    the pool (idle connections drop immediately; in-flight ones finish) and
    page the internal alert. Cheap: never opens a DB session itself.
    """
    global _pool_pressure_streak
    try:
        from .db import pool_status, dispose_pool
        st = pool_status()
        if st.get("dialect") == "sqlite":
            _pool_pressure_streak = 0
            return
        pressure = bool(st.get("pressure"))
        # Feed Sovereign circuit breaker (auto-pause) so heavy work cools even
        # when no sovereign runner is currently scheduled.
        try:
            from .sovereign_guard import note_pool_observation, pool_too_hot
            note_pool_observation(
                hot=pressure or pool_too_hot(),
                detail=f"db_pool_watchdog pressure={pressure}",
            )
        except Exception:
            pass
        if pressure:
            _pool_pressure_streak += 1
            logger.warning(
                "db_pool_watchdog: pressure streak=%s status=%s",
                _pool_pressure_streak, st,
            )
        else:
            if _pool_pressure_streak:
                logger.info("db_pool_watchdog: pressure cleared (was streak=%s)",
                            _pool_pressure_streak)
            _pool_pressure_streak = 0
            return
        if _pool_pressure_streak < 2:
            return
        # Wedged — dispose and alert once, then reset streak so we don't thrash.
        result = dispose_pool(reason=f"watchdog streak={_pool_pressure_streak}")
        _pool_pressure_streak = 0
        send_internal_alert(
            "DB pool disposed by watchdog (was near exhaustion)",
            "The SQLAlchemy connection pool stayed near capacity for ~2 minutes. "
            "The watchdog disposed the pool so new requests can check out fresh "
            "connections instead of hanging the whole API.\n\n"
            f"before={result.get('before')}\nafter={result.get('after')}\n\n"
            "If this fires repeatedly, hunt for a session held open across a "
            "slow vendor HTTP call or a runaway scheduler job.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("db_pool_watchdog failed: %s", exc)


def _run_freshness_scorecard() -> None:
    """Weekly measured-freshness scorecard to the internal alert address.
    Wrapper so import/build errors can't crash the scheduler."""
    try:
        from .jobs.freshness_scorecard import run_weekly_scorecard
        sc = run_weekly_scorecard()
        logger.info("freshness_scorecard: coverage=%s%% arrays=%s",
                    sc.get("headline_coverage_pct"), sc.get("arrays_total"))
    except Exception as exc:
        logger.exception("freshness_scorecard failed")
        send_internal_alert(
            "Freshness scorecard: unhandled exception",
            f"The weekly freshness scorecard raised an error:\n{exc}",
        )


def _run_generation_watchdog() -> None:
    """Daily billing-safety watchdog: alert if any physically impossible kWh row
    exists in DailyGeneration (billing meter) or InverterDaily. Read-only —
    alerts, never mutates."""
    try:
        from .jobs.generation_watchdog import run_generation_watchdog
        run_generation_watchdog()
    except Exception as exc:
        send_internal_alert(
            "Generation watchdog: unhandled exception",
            f"The billing-safety generation watchdog raised an error:\n{exc}",
        )


def _run_gmp_freshness_watchdog() -> None:
    """Weekly data-freshness watchdog: alert if any active GMP tenant has stopped
    capturing (extension dead / GMP login expired). Read-only — alerts, never
    mutates. GMP refreshes only via the in-browser extension, so a stale tenant
    means offtaker invoices + daily reports are built from frozen data."""
    try:
        from .jobs.gmp_freshness_watchdog import run_gmp_freshness_watchdog
        run_gmp_freshness_watchdog()
    except Exception as exc:
        send_internal_alert(
            "GMP freshness watchdog: unhandled exception",
            f"The GMP data-freshness watchdog raised an error:\n{exc}",
        )


def _run_cloud_capture_lockout_watchdog() -> None:
    """Hourly: alert while any Cloud Capture portal login sits at the lockout pause.

    Read-only — alerts, never mutates a credential. A paused login is still
    retried on a slow heartbeat; this is the loud half of that contract, so a
    stalled server-side capture can never go quiet the way the extension's
    background refresh once did (memory: no-self-sabotage-reliability-audit)."""
    try:
        from .harvester.lockout_alert import run_cloud_capture_watchdogs
        run_cloud_capture_watchdogs()
    except Exception as exc:
        send_internal_alert(
            "Cloud Capture lockout watchdog: unhandled exception",
            f"The Cloud Capture login lockout watchdog raised an error:\n{exc}",
        )


def _run_bill_discovery_drain() -> None:
    """Process queued bill-adapter discovery jobs (automatic portal explore)."""
    try:
        from .bill_discovery_engine import process_queued
        result = process_queued(limit=2)
        if result.get("processed"):
            logger.info("bill_discovery_drain: %s", result)
    except Exception as exc:
        logger.warning("bill_discovery_drain failed: %s", exc)


def _run_usage_report() -> None:
    """Report per-kWh usage for all Array Operator owners (metered billing)."""
    try:
        from .jobs.usage_report import report_usage_for_all_owners
        result = report_usage_for_all_owners()
        logger.info(
            "ao_usage_report: reported=%d skipped=%d errors=%d",
            len(result.get("reported", [])), result.get("skipped", 0),
            len(result.get("errors", [])),
        )
    except Exception as exc:
        send_internal_alert(
            "Array Operator usage report: unhandled exception",
            f"The per-kWh usage-report job raised an unexpected error:\n{exc}",
        )


def _run_nameplate_sync() -> None:
    """Sync per-kW nameplate billing quantity for all Array Operator owners."""
    try:
        from .jobs.nameplate_sync import sync_ao_nameplate_for_all_owners
        result = sync_ao_nameplate_for_all_owners()
        logger.info(
            "ao_nameplate_sync: synced=%d skipped=%d errors=%d",
            len(result.get("synced", [])), result.get("skipped", 0),
            len(result.get("errors", [])),
        )
    except Exception as exc:
        send_internal_alert(
            "Array Operator nameplate sync: unhandled exception",
            f"The per-kW nameplate-sync job raised an unexpected error:\n{exc}",
        )


def _run_gmp_daily_backfill() -> None:
    """Daily GMP daily-generation sponge top-up across every active tenant with
    enabled GMP accounts. Idempotent + incremental: only re-pulls the recent
    still-changing window plus any gaps, so day-to-day cost is tiny while the
    first run per meter walks its full multi-year history. This is what keeps
    Trends + Reports fed with GMP data hands-free. Wrapper so any import/build
    error can't crash the scheduler thread."""
    try:
        from sqlalchemy import select as _select
        from .jobs import gmp_daily_backfill as bf
        from .models import Tenant, UtilityAccount
        with SessionLocal() as db:
            tenant_ids = db.execute(
                _select(UtilityAccount.tenant_id)
                .where(UtilityAccount.provider == "gmp",
                       UtilityAccount.enabled == True,  # noqa: E712
                       UtilityAccount.deleted_at.is_(None))
                .group_by(UtilityAccount.tenant_id)
            ).scalars().all()
        ins = upd = errs = 0
        for tid in tenant_ids:
            r = bf.backfill_tenant(tid)
            t = r.get("totals", {})
            ins += t.get("daily_inserted", 0)
            upd += t.get("daily_updated", 0)
            errs += t.get("accounts_error", 0)
        logger.info(
            "gmp_daily_backfill: tenants=%d daily_inserted=%d daily_updated=%d account_errors=%d",
            len(tenant_ids), ins, upd, errs,
        )
    except Exception as exc:
        logger.exception("gmp_daily_backfill: crashed")
        send_internal_alert(
            "GMP daily backfill: unhandled exception",
            f"The scheduled GMP daily-generation backfill raised an error:\n{exc}",
        )


def _run_ea_reminders() -> None:
    """Fire due Energy Agent reminders + condition watches (per-tenant, only for
    tenants with active reminders). Wrapper so errors never crash the scheduler."""
    try:
        from sqlalchemy import distinct, select as _select
        from .db import SessionLocal
        from .energy_agent import EaReminder, evaluate_ea_reminders_for_tenant
        from .models import Tenant
        with SessionLocal() as db:
            tids = db.execute(
                _select(distinct(EaReminder.tenant_id)).where(EaReminder.status == "active")
            ).scalars().all()
        fired = 0
        for tid in tids[:200]:
            try:
                with SessionLocal() as db:
                    t = db.get(Tenant, tid)
                    if t is not None:
                        fired += evaluate_ea_reminders_for_tenant(db, t)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ea reminders tenant %s: %s", tid, exc)
        if fired:
            logger.info("ea reminders fired: %d", fired)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ea reminders job failed: %s", exc)


def _run_inbound_email_sync() -> None:
    """Safety-net poll of Resend receiving (webhook-miss recovery). Deduped —
    safe to run repeatedly; one short session per run."""
    try:
        from .db import SessionLocal
        from .repair_ops import sync_inbound_from_resend
        with SessionLocal() as db:
            out = sync_inbound_from_resend(db, limit=25)
            db.commit()
        if out.get("matched_new") or any(
            r.get("owner_agent") and not r.get("deduped") for r in out.get("results", [])
        ):
            logger.info("inbound_email_sync recovered mail: %s", {
                k: out.get(k) for k in ("scanned", "processed", "matched_new", "deduped")
            })
    except Exception as exc:
        logger.warning("inbound_email_sync failed: %s", exc)


def _run_ea_weekly_checkin() -> None:
    """Monday agentic owner check-in (Energy Agent writes each owner a
    first-person weekly note; replies come back through the agent mailbox).
    Wrapper so import/compose errors never crash the scheduler at fire time."""
    try:
        from .energy_agent_email import run_weekly_checkins
        result = run_weekly_checkins()
        logger.info(
            "ea_weekly_checkin: sent=%d skipped=%d failed=%d",
            len(result.get("sent", [])), len(result.get("skipped", [])),
            len(result.get("failed", [])),
        )
    except Exception as exc:
        send_internal_alert(
            "EA weekly check-in: unhandled exception",
            f"The weekly owner check-in job raised:\n{exc}",
        )


def _run_morning_fleet_digest() -> None:
    """Daily tenant-facing morning fleet-health digest for Array Operator owners.
    Wrapper so import/build errors don't crash the scheduler at fire time."""
    try:
        from .jobs.morning_fleet_digest import run_morning_digest
        result = run_morning_digest()
        logger.info(
            "morning_fleet_digest: sent=%d skipped=%d errors=%d",
            len(result.get("sent", [])), result.get("skipped", 0),
            len(result.get("errors", [])),
        )
    except Exception as exc:
        send_internal_alert(
            "Morning fleet digest: unhandled exception",
            f"The morning fleet-health digest job raised an unexpected error:\n{exc}",
        )


def _run_verification_monthly_reports() -> None:
    """1st-of-month Performance Verification pack for Array Operator owners."""
    try:
        from .jobs.verification_monthly import run_monthly_verification_reports
        result = run_monthly_verification_reports()
        logger.info(
            "verification_monthly_reports: sent=%d skipped=%d errors=%d",
            len(result.get("sent", [])), result.get("skipped", 0),
            len(result.get("errors", [])),
        )
    except Exception as exc:
        send_internal_alert(
            "Monthly verification reports: unhandled exception",
            f"The verification_monthly_reports job raised an unexpected error:\n{exc}",
        )


def _run_morning_digest_watchdog() -> None:
    """If the morning digest did not run today, alert — don't stay silent.

    The digest job stamps a KVFlag heartbeat (morning_digest:last_run) on every
    execution. If today's date is missing from that heartbeat when this runs
    (~3h later), the noon job never fired (missed trigger / early crash), which is
    exactly the failure that had no trace before. Turn it into a visible alert."""
    try:
        import json
        from datetime import datetime, timezone
        from .db import SessionLocal
        from .models import KVFlag
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with SessionLocal() as db:
            row = db.get(KVFlag, "morning_digest:last_run")
            last = {}
            if row and row.value:
                try:
                    last = json.loads(row.value)
                except Exception:
                    last = {}
            if last.get("date") == today:
                logger.info("morning_digest_watchdog: ok (ran today: %s)", last)
                return
        send_internal_alert(
            "Morning digest did NOT run today",
            "The 12:00 UTC morning fleet digest has no run-heartbeat for "
            f"{today} (last heartbeat: {last or 'none'}). It likely missed its "
            "trigger (restart/misfire) or crashed before sending. No customer "
            "digests went out this morning — investigate the scheduler.",
        )
    except Exception as exc:
        logger.warning("morning_digest_watchdog failed: %s", exc)


def _run_delivery_receipts() -> None:
    """Post-send delivery receipt to NEPOOL operators (what went out + confirmed
    delivered). Wrapper so import/build errors can't crash the scheduler."""
    try:
        from .jobs.report_digests import run_delivery_receipts
        result = run_delivery_receipts()
        if result.get("operators"):
            logger.info("report_delivery_receipts: operators=%d rows=%d",
                        result.get("operators", 0), result.get("rows", 0))
    except Exception as exc:
        send_internal_alert(
            "Report delivery receipts: unhandled exception",
            f"The post-send delivery-receipt job raised an unexpected error:\n{exc}",
        )


def _run_presend_reviews() -> None:
    """2-day-ahead 'here's what will go out, review it' digest to NEPOOL operators.
    Wrapper so import/build errors can't crash the scheduler."""
    try:
        from .jobs.report_digests import run_presend_reviews
        result = run_presend_reviews()
        if result.get("operators"):
            logger.info("report_presend_reviews: operators=%d cadences=%s",
                        result.get("operators", 0), result.get("cadences"))
    except Exception as exc:
        send_internal_alert(
            "Report pre-send reviews: unhandled exception",
            f"The 2-day-ahead report review job raised an unexpected error:\n{exc}",
        )


def _run_new_bill_reviews() -> None:
    """When a new GMP bill lands for an offtaker, email the Array Operator a
    'your next invoice is ready to review' prompt. Daily sweep, deduped per bill
    period. Wrapper so import/build errors can't crash the scheduler."""
    try:
        from .jobs.new_bill_review import run_new_bill_reviews
        result = run_new_bill_reviews()
        if result.get("emailed"):
            logger.info("new_bill_reviews: emailed=%d candidates=%d",
                        result.get("emailed", 0), len(result.get("candidates", [])))
    except Exception as exc:
        send_internal_alert(
            "New-bill review prompts: unhandled exception",
            f"The 'come review your next bill' job raised an unexpected error:\n{exc}",
        )


def _run_inverter_alert_sweep() -> None:
    """Email Array Operator owners about down/underperforming inverters.

    Reuses build_fleet_tree truth and de-dups via InverterAlertState so each
    incident emails once (after the owner's grace window), then stays quiet
    until the inverter recovers and trips again.
    """
    try:
        from .inverter_alert_sweep import run_sweep
        result = run_sweep()
        logger.info(
            "inverter_alert_sweep: tenants_swept=%d inverters_emailed=%d",
            result.get("tenants_swept", 0), result.get("inverters_emailed", 0),
        )
    except Exception as exc:
        send_internal_alert(
            "Inverter alert sweep: unhandled exception",
            f"The inverter down/underperformance alert sweep raised an "
            f"unexpected error:\n{exc}",
        )


def _run_inverter_pull() -> None:
    """Pull daily generation for every inverter connection (all vendors)."""
    try:
        from .jobs.inverter_pull import pull_all_inverters
        result = pull_all_inverters()
        logger.info(
            "inverter_daily_pull: processed=%d", result.get("connections_processed", 0)
        )
    except Exception as exc:
        send_internal_alert(
            "Inverter daily pull: unhandled exception",
            f"The inverter daily pull job raised an unexpected error:\n{exc}",
        )


def _run_history_heal() -> None:
    """Self-healing deep-history backfill: fill multi-year history for any
    inverter connection that hasn't had it pulled yet (so Trends shows past
    years, not just the ~90 days the nightly pull reaches)."""
    try:
        from .jobs.inverter_history import heal_missing_history
        result = heal_missing_history()
        logger.info(
            "inverter_history_heal: processed=%d stamped=%d still_pending=%d",
            result.get("processed", 0), result.get("stamped", 0),
            result.get("still_pending", 0),
        )
    except Exception as exc:
        send_internal_alert(
            "Inverter history heal: unhandled exception",
            f"The deep-history backfill heal job raised an unexpected error:\n{exc}",
        )


def _run_array_dedup() -> None:
    """Auto-merge unambiguous duplicate arrays (GMP↔vendor twins). Lossless +
    undo-logged; questionable pairs are left as one-click suggestions."""
    try:
        from .jobs.array_dedup import sweep_all_tenants
        result = sweep_all_tenants(execute=True)
        logger.info(
            "array_dedup_sweep: auto_merged=%d suggested=%d across %d tenants",
            result.get("auto_merged", 0), result.get("suggested", 0),
            result.get("tenants_with_dupes", 0),
        )
    except Exception as exc:
        send_internal_alert(
            "Array dedup sweep: unhandled exception",
            f"The duplicate-array auto-merge job raised an unexpected error:\n{exc}",
        )


def _run_inverter_history_snapshot() -> None:
    """Snapshot per-inverter daily history into InverterDaily for every owner so the
    graphs keep accumulating real history (API-independent) even with no dashboard
    traffic. Critical for SolarEdge (otherwise live-API-only per-inverter telemetry)."""
    try:
        from .jobs.inverter_history_snapshot import snapshot_all_inverter_history
        result = snapshot_all_inverter_history()
        logger.info(
            "inverter_history_snapshot: tenants=%d inverters=%d errors=%d",
            result.get("tenants_processed", 0), result.get("inverters_seen", 0),
            len(result.get("errors", [])),
        )
    except Exception as exc:
        send_internal_alert(
            "Inverter history snapshot: unhandled exception",
            f"The per-inverter history snapshot job raised an unexpected error:\n{exc}",
        )


def _run_synthetic_gmp_monitor() -> None:
    """Wrapper so import errors don't crash the scheduler at start() time."""
    try:
        from scripts.synthetic_gmp_monitor import run as synthetic_run
        synthetic_run()
    except Exception as exc:
        send_internal_alert(
            "Synthetic GMP monitor: unhandled exception",
            f"The synthetic_gmp_monitor job raised an unexpected error:\n{exc}",
        )
