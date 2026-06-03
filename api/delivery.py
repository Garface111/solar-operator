"""
Solar Operator — workbook delivery.

Single source of truth for "build workbook + email it." Always regenerates
from solar.db, writes to a temp file, attaches via Resend, cleans up.

Used by:
  - /v1/account/send-report (customer self-serve)
  - /admin/tenants/{tid}/deliver (ops force-send)
  - scheduler.deliver_scheduled_reports (weekly/monthly/quarterly cron)
"""
from __future__ import annotations
import logging
import pathlib
import tempfile
from datetime import datetime
from typing import Optional

from .db import SessionLocal
from .models import Tenant, now
from .writers import build_workbook
from .notify import send_workbook_email, send_internal_alert

logger = logging.getLogger(__name__)


def deliver_for_tenant(tenant_id: str, *, year: Optional[int] = None,
                       override_to: Optional[str] = None,
                       triggered_by: str = "manual") -> dict:
    """Generate the default workbook for one tenant and email it.

    Builds into a temp directory that's wiped after the email is sent, so
    Railway's ephemeral disk never accumulates stale .xlsx files. The
    workbook can always be regenerated from solar.db.
    """
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            raise ValueError(f"Tenant not found: {tenant_id}")
        recipient = override_to or t.contact_email
        tenant_name = t.name
        is_active = t.active or t.subscription_status in ("comped", "trialing")

    if not is_active and triggered_by != "ops":
        return {"ok": False, "reason": "tenant not active", "tenant": tenant_id}

    year = year or datetime.utcnow().year

    with tempfile.TemporaryDirectory(prefix=f"so-deliver-{tenant_id}-") as tmpdir:
        out_path = pathlib.Path(tmpdir) / f"{year}-monthly-kwh.xlsx"
        try:
            path = build_workbook(tenant_id, year=year, out_path=out_path)
        except Exception as e:
            logger.exception("Workbook build failed for %s", tenant_id)
            send_internal_alert(
                "Workbook generation failed",
                f"Tenant: {tenant_id} ({tenant_name})\nYear: {year}\n"
                f"Triggered by: {triggered_by}\nError: {e}",
            )
            return {"ok": False, "tenant": tenant_id, "error": str(e)}

        first_name = tenant_name.split()[0] if tenant_name else "there"
        sent = send_workbook_email(
            to=recipient,
            subject=f"Your Solar Operator NEPOOL-GIS quarterly report",
            html=(f"<p>Hi {first_name},</p>"
                  f"<p>Your latest NEPOOL-GIS quarterly generation workbook "
                  f"is attached. It covers the last 6 complete quarters of "
                  f"generation data we have on file through "
                  f"{datetime.utcnow():%B %d, %Y}, one sheet per array.</p>"
                  f"<p>Manage your account, change report frequency, or "
                  f"cancel anytime at "
                  f"<a href='https://solaroperator.org/account.html'>"
                  f"solaroperator.org/account</a>.</p>"
                  f"<p>Questions? Just reply.</p>"
                  f"<p>— Solar Operator</p>"),
            text=(f"Hi {tenant_name},\n\n"
                  f"Your latest NEPOOL-GIS quarterly generation workbook "
                  f"is attached (through {datetime.utcnow():%B %d, %Y}).\n\n"
                  f"Manage your account at https://solaroperator.org/account.html\n\n"
                  f"Questions? Just reply.\n\n— Solar Operator"),
            workbook_path=str(path),
            filename=f"{tenant_name.replace(' ', '_')}-GMCS-report.xlsx",
        )

    # Update last_delivery_at marker
    if sent and not override_to:
        with SessionLocal() as db:
            t = db.get(Tenant, tenant_id)
            if t:
                t.last_delivery_at = now()
                db.commit()

    return {
        "ok": True,
        "tenant": tenant_id,
        "email_sent": sent,
        "recipient": recipient,
        "year": year,
        "triggered_by": triggered_by,
    }
