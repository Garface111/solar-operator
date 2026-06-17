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


def _deliver_clients_with_frequency(frequency: str) -> dict:
    """Send the workbook to every active CLIENT whose effective frequency
    matches. Effective = client.report_frequency if set, else
    tenant.report_frequency. Skips clients of inactive non-comped tenants.
    """
    from .delivery import deliver_for_client
    from .notify import send_internal_alert

    sent: list[int] = []
    failed: list[int] = []
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

    for cid in candidates:
        try:
            result = deliver_for_client(cid, triggered_by=f"sched-{frequency}")
            (sent if result.get("ok") and result.get("email_sent") else failed).append(cid)
        except Exception as e:
            failed.append(cid)
            send_internal_alert(
                f"Scheduled delivery failed ({frequency})",
                f"Client: {cid}\nError: {e}",
            )

    if failed:
        send_internal_alert(
            f"Scheduled delivery — partial failures ({frequency})",
            f"Sent OK: {sent}\nFailed: {failed}",
        )
    return {"frequency": frequency, "sent": sent, "failed": failed}


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
    from .billing.delivery import deliver_subscription

    sent: list[int] = []
    failed: list[int] = []
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
            try:
                result = deliver_subscription(
                    db, sub, tenant,
                    triggered_by=f"sched-billing-{'trueup' if trueup_only else cadence}")
                (sent if result.get("ok") else failed).append(sid)
                if not result.get("ok"):
                    logger.warning("billing delivery skipped sub %s: %s",
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
            f"Sent OK: {sent}\nFailed/skipped: {failed}",
        )
    return {"cadence": cadence, "trueup_only": trueup_only,
            "sent": sent, "failed": failed}


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
                    array_price_id_for_product, is_array_operator,
                )
                ao = is_array_operator(product)
                price_id = array_price_id_for_product(product)
                items = []
                if ao:
                    # Array Operator = per-kWh METERED line, NO quantity, NO setup
                    # fee. Usage is reported by report_usage_for_all_owners().
                    if price_id:
                        items.append({"price": price_id})
                else:
                    if setup_price_id:
                        items.append({"price": setup_price_id, "quantity": 1})
                    if price_id:
                        items.append({"price": price_id, "quantity": quantity})
                sub = stripe.Subscription.create(
                    customer=t.stripe_customer_id,
                    items=items if items else None,
                    default_payment_method=t.stripe_payment_method_id,
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
    """Remind no-card trialing operators ~3 days before their trial ends.

    Runs once daily. Selects trialing tenants with NO payment method on file
    whose trial_ends_at is within 3 days AND who haven't already been reminded
    (trial_reminder_sent_at IS NULL). After sending we stamp
    trial_reminder_sent_at, making the reminder exactly-once regardless of tick
    cadence — a missed or double-fired tick no longer drops or duplicates it.
    Tenants who already added a card go through the normal trial-charge path and
    are skipped here.
    """
    if not os.getenv("STRIPE_SECRET_KEY"):
        # Mirrors finalize_expired_trials: skip when billing isn't configured.
        return {"reminded": []}
    window_end = datetime.utcnow() + timedelta(days=3)
    reminded: list[str] = []
    with SessionLocal() as db:
        rows = db.execute(
            select(Tenant).where(
                Tenant.subscription_status == "trialing",
                Tenant.stripe_payment_method_id.is_(None),
                Tenant.trial_ends_at <= window_end,
                Tenant.trial_reminder_sent_at.is_(None),
            )
        ).scalars().all()
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
                # leaves the tenant eligible for the next tick (at-least-once on
                # failure, exactly-once on success).
                t = db.get(Tenant, tid)
                if t is not None:
                    t.trial_reminder_sent_at = datetime.utcnow()
                    db.commit()
                reminded.append(tid)
            except Exception as e:
                db.rollback()
                logger.warning(
                    "send_trial_ending_no_card_reminder_email failed for %s: %s",
                    tid, e)
    logger.info("send_trial_ending_reminders: reminded=%d", len(reminded))
    return {"reminded": reminded}


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
                sess.refresh_failures = (sess.refresh_failures or 0) + 1
                db.commit()
                logger.warning(
                    "GMP refresh failed: tenant=%s sess=%d failures=%d err=%s",
                    sess.tenant_id, sess.id, sess.refresh_failures, exc,
                )
                failed.append(sess.id)
                if sess.refresh_failures >= 3 and tenant:
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
                        f"GMP refresh: 3 failures for tenant {sess.tenant_id}",
                        f"Tenant: {sess.tenant_id} ({getattr(tenant, 'contact_email', '?')})\n"
                        f"Session: {sess.id}\nToken prefix: {token_prefix}...\n"
                        f"Operator notified to re-login.",
                    )

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
    # Weekly: Mondays at 09:00 UTC
    scheduler.add_job(
        deliver_weekly_reports,
        CronTrigger(day_of_week="mon", hour=9, minute=0),
        id="deliver_weekly", replace_existing=True,
    )
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
    # Daily at 03:30 UTC: snapshot per-inverter daily history into InverterDaily for
    # every owner (persist-on-read forced on a schedule) so the per-inverter graphs
    # keep accumulating real history even when nobody opens the dashboard. Critical
    # for SolarEdge, whose per-inverter telemetry is otherwise live-API-only.
    scheduler.add_job(
        _run_inverter_history_snapshot,
        CronTrigger(hour=3, minute=30),
        id="inverter_history_snapshot", replace_existing=True,
    )
    # Daily at 04:00 UTC: report Array Operator per-kWh usage to Stripe (metered
    # billing). Runs AFTER the 03:00 inverter pull so the day's kWh are landed.
    scheduler.add_job(
        _run_usage_report,
        CronTrigger(hour=4, minute=0),
        id="ao_usage_report", replace_existing=True,
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
    scheduler.start()


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
