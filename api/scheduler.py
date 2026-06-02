"""
APScheduler — runs pull-bills on a cadence so new monthly bills land
automatically. Also fires monthly default-template deliveries.

Schedule:
  - every 6 hours: enqueue pull_bills jobs for all active tenants
  - every 1 minute: drain the job queue
  - 1st of every month at 09:00 UTC: deliver default-template workbooks
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


def deliver_monthly_default_reports():
    """Generate + email the default workbook to every active tenant.
    No template layer — every customer gets the same arrays×months format,
    populated from their own bills."""
    from datetime import datetime as _dt
    # Lazy imports to avoid circulars at module load
    from .writers import build_workbook
    from .notify import send_workbook_email, send_internal_alert

    sent = []
    failed = []
    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(Tenant.active == True)
        ).scalars().all()

    year = _dt.utcnow().year
    for tenant in tenants:
        try:
            path = build_workbook(tenant.id, year=year)
            ok = send_workbook_email(
                to=tenant.contact_email,
                subject=f"Your {year} Solar Operator monthly kWh report",
                html=(f"<p>Hi {tenant.name.split()[0] if tenant.name else 'there'},</p>"
                      f"<p>Your latest monthly kWh workbook is attached.</p>"
                      f"<p>— Solar Operator</p>"),
                text=f"Hi {tenant.name},\n\nYour latest monthly kWh workbook is attached.\n\n— Solar Operator",
                workbook_path=str(path),
                filename=f"{tenant.name.replace(' ', '_')}-{year}-monthly-kwh.xlsx",
            )
            (sent if ok else failed).append(tenant.id)
        except Exception as e:
            failed.append(tenant.id)
            send_internal_alert(
                "Monthly default delivery failed",
                f"Tenant: {tenant.id} ({tenant.name})\nError: {e}",
            )

    if failed:
        send_internal_alert(
            "Monthly default delivery — partial failures",
            f"Sent OK: {sent}\nFailed: {failed}",
        )
    return {"sent": sent, "failed": failed}


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
    # 1st of every month at 09:00 UTC — deliver default-template reports
    scheduler.add_job(
        deliver_monthly_default_reports,
        CronTrigger(day=1, hour=9, minute=0),
        id="deliver_monthly_default", replace_existing=True,
    )
    scheduler.start()
