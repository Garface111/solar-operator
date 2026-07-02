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
import logging
import os
from datetime import datetime, timedelta

import stripe as stripe

logger = logging.getLogger(__name__)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, or_, func, text
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

scheduler = BackgroundScheduler(timezone="UTC")


def enqueue_pull_for_all_tenants():
    with SessionLocal() as db:
        tenants = db.execute(select(Tenant).where(Tenant.active == True)).scalars().all()
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

    sent: list[int] = []
    failed: list[int] = []
    skipped_empty: list[int] = []
    results: list[dict] = []  # full per-client outcome → operator delivery receipt
    with SessionLocal() as db:
        # All client rows that EITHER explicitly match the cadence OR
        # inherit it from the tenant
        rows = db.execute(
            select(Client, Tenant)
            .join(Tenant, Client.tenant_id == Tenant.id)
            .where(Client.active == True)  # noqa: E712
            .where(
                or_(
                    Client.report_frequency == frequency,
                    (Client.report_frequency.is_(None)) &
                    (Tenant.report_frequency == frequency),
                )
            )
        ).all()
        candidates = [
            c.id for (c, t) in rows
            if (t.active or t.subscription_status in ("comped", "trialing"))
        ]
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


def deliver_billing_reports(cadence: str, *, trueup_only: bool = False) -> dict:
    """Array Operator automatic billing reports — deliver every enabled
    BillingReportSubscription whose cadence matches (or, for the annual run,
    every sub with annual_trueup set).

    Mirrors _deliver_clients_with_frequency: skips subs of inactive non-comped
    tenants, internal-alerts on failure, exactly-once stamping happens inside
    deliver_subscription (it sets last_sent_at / next_send_at on success only)."""
    from .models import BillingReportSubscription
    from .billing.delivery import deliver_subscription, draft_subscription

    sent: list[int] = []
    drafted: list[int] = []
    failed: list[int] = []
    skipped: list[int] = []   # benign no-ops (waiting on a bill, or already sent
                              # this period) — NOT failures; don't alert on them.
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
            # #18: annual true-up has no real banked-vs-cashed reconciliation for
            # utility-bill offtakers — the normal delivery path would send a single
            # MONTH's invoice mislabeled as an annual true-up (and burn an invoice
            # number). Skip them (honest no-op) until a real annual reconciliation
            # exists, rather than imply a capability that isn't there.
            if trueup_only and getattr(sub, "utility_account_id", None) is not None:
                logger.info(
                    "annual true-up skipped for utility-bill offtaker sub %s — no "
                    "annual reconciliation implemented (would mis-send one month)", sid)
                continue
            # Per-customer choice: "approval" (default) drafts into the operator's
            # inbox for review-and-send; "auto" sends straight to the recipient.
            mode = getattr(sub, "delivery_mode", "approval") or "approval"
            try:
                if mode == "auto":
                    result = deliver_subscription(
                        db, sub, tenant,
                        triggered_by=f"sched-billing-{'trueup' if trueup_only else cadence}")
                    if result.get("ok"):
                        sent.append(sid)
                    elif result.get("skipped"):
                        skipped.append(sid)   # waiting on bill / already sent — benign
                    else:
                        failed.append(sid)
                else:
                    result = draft_subscription(
                        db, sub, tenant,
                        triggered_by=f"sched-draft-{'trueup' if trueup_only else cadence}")
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
            f"Failed: {failed}",
        )
    return {"cadence": cadence, "trueup_only": trueup_only,
            "sent": sent, "drafted": drafted, "failed": failed, "skipped": skipped}


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
                    send_add_first_array_email(
                        to=t.contact_email, name=t.operator_name or t.company_name or t.name)
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
                    send_trial_paused_no_card_email(
                        to=t.contact_email,
                        name=t.operator_name or t.company_name or t.name,
                        product=getattr(t, "product", "nepool"))
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
                    to=email, name=name, trial_end_date=end_str, product=product)
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
    return {"refreshed": refreshed, "failed": failed, "skipped": skipped}


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
    except Exception:
        logger.exception("poller: poll_all_sources crashed")
        return {"ran": False, "error": "exception"}


def prune_inverter_readings_job() -> dict:
    from .poller import prune_old_readings
    try:
        return prune_old_readings()
    except Exception:
        logger.exception("poller: prune_old_readings crashed")
        return {"pruned": 0, "error": "exception"}


def start():
    # Every 6 hours, enqueue pull-bills jobs for each active tenant
    scheduler.add_job(
        enqueue_pull_for_all_tenants,
        "interval", hours=6, id="enqueue_pull_bills", replace_existing=True,
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
    # Weekly: Mondays at 09:00 UTC
    scheduler.add_job(
        deliver_weekly_reports,
        CronTrigger(day_of_week="mon", hour=9, minute=0),
        id="deliver_weekly", replace_existing=True,
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
    # Daily at 03:05 UTC: pull daily generation for every SmartHub (VEC/WEC/
    # Stowe/…) array that has a captured session token. SmartHub bills carry NO
    # generation kWh — a net-metering solar account's production lives only in
    # the usage API — so this server-side pull is what feeds DailyGeneration
    # (and therefore Trends + NEPOOL reports) for those accounts. Skips arrays
    # without a stored token (extension must capture the authorizationToken).
    scheduler.add_job(
        _run_smarthub_pull,
        CronTrigger(hour=3, minute=5),
        id="smarthub_daily_pull", replace_existing=True,
        max_instances=1, coalesce=True,
    )
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
    # Weekly (Mon 13:30 UTC): GMP data-freshness watchdog — alert if any active GMP
    # tenant has stopped capturing (extension dead / GMP login expired), so we never
    # silently bill or report from frozen data. Weekly cadence avoids alert fatigue.
    scheduler.add_job(
        _run_gmp_freshness_watchdog,
        CronTrigger(day_of_week="mon", hour=13, minute=30),
        id="gmp_freshness_watchdog", replace_existing=True,
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
    scheduler.start()


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


def _run_smarthub_pull() -> None:
    """Pull daily generation for every SmartHub (VEC/WEC/Stowe/…) array that has
    a captured session token. SmartHub bills carry no generation kWh, so this
    server-side usage-API pull is the authoritative DailyGeneration source for
    those net-metering accounts."""
    try:
        from .jobs.smarthub_pull import pull_all_smarthub
        result = pull_all_smarthub()
        logger.info(
            "smarthub_daily_pull: processed=%d with_data=%d inserted=%d "
            "updated=%d skipped=%d errors=%d",
            result.get("arrays_processed", 0), result.get("arrays_with_data", 0),
            result.get("inserted", 0), result.get("updated", 0),
            result.get("skipped", 0), result.get("errors", 0),
        )
    except Exception as exc:
        send_internal_alert(
            "SmartHub daily pull: unhandled exception",
            f"The SmartHub daily generation pull job raised an unexpected error:\n{exc}",
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
