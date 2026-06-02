"""
APScheduler — runs pull-bills on a cadence so new monthly bills land
automatically. Also fires per-tenant report deliveries based on
each tenant's report_frequency.

Schedule:
  - every 6 hours: enqueue pull_bills jobs for all active tenants
  - every 1 minute: drain the job queue
  - every Monday at 09:00 UTC: deliver to weekly tenants
  - 1st of every month at 09:00 UTC: deliver to monthly tenants
  - 1st of Jan/Apr/Jul/Oct at 09:00 UTC: deliver to quarterly tenants
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from .db import SessionLocal
from .models import Tenant, Job, now

scheduler = BackgroundScheduler(timezone="UTC")


def enqueue_pull_for_all_tenants():
    with SessionLocal() as db:
        tenants = db.execute(select(Tenant).where(Tenant.active == True)).scalars().all()
        for t in tenants:
            db.add(Job(tenant_id=t.id, kind="pull_bills", payload={}, status="queued"))
        db.commit()
    return len(tenants)


def _deliver_to_tenants_with_frequency(frequency: str) -> dict:
    """Send the default workbook to every active tenant whose report_frequency
    matches. Comped tenants (active=False but status=comped) get reports too."""
    from .delivery import deliver_for_tenant
    from .notify import send_internal_alert

    sent = []
    failed = []
    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(Tenant.report_frequency == frequency)
        ).scalars().all()
        candidates = [
            t.id for t in tenants
            if t.active or t.subscription_status in ("comped", "trialing")
        ]

    for tid in candidates:
        try:
            result = deliver_for_tenant(tid, triggered_by=f"sched-{frequency}")
            (sent if result.get("ok") and result.get("email_sent") else failed).append(tid)
        except Exception as e:
            failed.append(tid)
            send_internal_alert(
                f"Scheduled delivery failed ({frequency})",
                f"Tenant: {tid}\nError: {e}",
            )

    if failed:
        send_internal_alert(
            f"Scheduled delivery — partial failures ({frequency})",
            f"Sent OK: {sent}\nFailed: {failed}",
        )
    return {"frequency": frequency, "sent": sent, "failed": failed}


def deliver_weekly_reports():
    return _deliver_to_tenants_with_frequency("weekly")


def deliver_monthly_reports():
    return _deliver_to_tenants_with_frequency("monthly")


def deliver_quarterly_reports():
    return _deliver_to_tenants_with_frequency("quarterly")


def start():
    # Every 6 hours, enqueue pull-bills jobs for each active tenant
    scheduler.add_job(
        enqueue_pull_for_all_tenants,
        "interval", hours=6, id="enqueue_pull_bills", replace_existing=True,
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
    scheduler.start()
