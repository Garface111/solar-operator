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
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, or_, func, text
from .db import SessionLocal, engine
from .models import Tenant, Client, Array, Job, now

scheduler = BackgroundScheduler(timezone="UTC")


def enqueue_pull_for_all_tenants():
    with SessionLocal() as db:
        tenants = db.execute(select(Tenant).where(Tenant.active == True)).scalars().all()
        for t in tenants:
            db.add(Job(tenant_id=t.id, kind="pull_bills", payload={}, status="queued"))
        db.commit()
    return len(tenants)


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


def finalize_expired_trials():
    """Convert expired trials to live subscriptions (or extend zero-array trials).

    Runs hourly. For each tenant in 'trialing' state whose trial_ends_at has
    passed:
      - If they have no arrays yet and haven't been extended: add 3 days,
        send the 'add your first array' email, and leave them trialing.
      - Otherwise: create the Stripe subscription on the stored payment method
        (quantity = actual array count, minimum 1), mark active, clear trial.
    """
    import stripe
    from .notify import send_add_first_array_email, send_trial_charged_email, send_internal_alert

    stripe_secret = os.getenv("STRIPE_SECRET_KEY", "")
    setup_price_id = os.getenv("STRIPE_SETUP_PRICE_ID", "")
    array_price_id = os.getenv("STRIPE_ARRAY_PRICE_ID", "")

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
                        to=t.contact_email, name=t.name)
                except Exception:
                    pass
                send_internal_alert(
                    f"Trial extended (no arrays): {t.id}",
                    f"Tenant {t.id} ({t.contact_email}) had 0 arrays at trial end. "
                    f"Extended 3 days."
                )
                continue

            # Charge the card.
            quantity = max(array_count, 1)
            try:
                items = []
                if setup_price_id:
                    items.append({"price": setup_price_id, "quantity": 1})
                if array_price_id:
                    items.append({"price": array_price_id, "quantity": quantity})
                sub = stripe.Subscription.create(
                    customer=t.stripe_customer_id,
                    items=items if items else None,
                    default_payment_method=t.stripe_payment_method_id,
                )
                sub_id = sub["id"]
                t.stripe_subscription_id = sub_id
                t.subscription_status = "active"
                t.trial_ends_at = None
                db.commit()

                # Estimate the charge for the confirmation email. Pull the
                # latest invoice amount from Stripe rather than guessing.
                amount_dollars = 0.0
                try:
                    inv = stripe.Invoice.retrieve(sub.get("latest_invoice") or "")
                    amount_dollars = (inv.get("amount_due") or 0) / 100
                except Exception:
                    pass

                try:
                    send_trial_charged_email(
                        to=t.contact_email, name=t.name,
                        array_count=quantity, amount_dollars=amount_dollars)
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
    # Drain the queue every minute
    from .worker import run_pending_jobs
    scheduler.add_job(
        run_pending_jobs, "interval", minutes=1, id="run_pending_jobs", replace_existing=True,
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
    # Daily at 03:00 UTC: hard-delete rows soft-deleted > 30 days ago
    scheduler.add_job(
        hard_delete_old_soft_deleted,
        CronTrigger(hour=3, minute=0),
        id="hard_delete_old", replace_existing=True,
    )
    scheduler.start()
