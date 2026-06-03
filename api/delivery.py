"""
Solar Operator — workbook delivery.

Single source of truth for "build workbook + email it." Always regenerates
from solar.db, writes to a temp file, attaches via Resend, cleans up.

Phase-1 expansion (June 2026):
  Reports are now generated PER CLIENT, not per tenant. A tenant with N
  clients gets N separate emails (one workbook per client, each delivered
  to that client's own contact_email + cc_emails).

Used by:
  - /v1/account/send-report (customer self-serve; sends ALL clients)
  - /admin/tenants/{tid}/deliver (ops force-send all clients)
  - /admin/clients/{cid}/deliver (ops force-send one client)
  - scheduler.deliver_scheduled_reports (weekly/monthly/quarterly cron)
"""
from __future__ import annotations
import logging
import pathlib
import tempfile
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, Client, now
from .writers import build_workbook
from .notify import send_workbook_email, send_internal_alert

logger = logging.getLogger(__name__)


def _recipients_for_client(client: Client, tenant: Tenant,
                           override_to: Optional[str]) -> list[str]:
    """Resolve who should receive this client's report.
    Order of preference: override > client.contact_email > tenant.contact_email.
    Always dedupes and includes client.cc_emails when present."""
    if override_to:
        return [override_to]
    primary = client.contact_email or tenant.contact_email
    addrs: list[str] = []
    if primary:
        addrs.append(primary.strip())
    if client.cc_emails:
        for x in client.cc_emails.split(","):
            x = x.strip()
            if x and x not in addrs:
                addrs.append(x)
    return addrs


def deliver_for_client(client_id: int, *, year: Optional[int] = None,
                       override_to: Optional[str] = None,
                       triggered_by: str = "manual") -> dict:
    """Build & email the workbook for ONE client."""
    with SessionLocal() as db:
        client = db.get(Client, client_id)
        if not client:
            raise ValueError(f"Client not found: {client_id}")
        tenant = db.get(Tenant, client.tenant_id)
        if not tenant:
            raise ValueError(f"Tenant not found for client {client_id}")
        is_active = (tenant.active or tenant.subscription_status in ("comped", "trialing")) and client.active
        client_name = client.name
        tenant_name = tenant.name
        tenant_id = tenant.id

    if not is_active and triggered_by != "ops":
        return {"ok": False, "reason": "tenant or client inactive",
                "client_id": client_id, "tenant": tenant_id}

    recipients = _recipients_for_client(client, tenant, override_to)
    if not recipients:
        return {"ok": False, "reason": "no recipient email on file",
                "client_id": client_id, "tenant": tenant_id}

    safe_client = client_name.replace(" ", "_").replace("/", "-")
    with tempfile.TemporaryDirectory(prefix=f"so-deliver-c{client_id}-") as tmpdir:
        out_path = pathlib.Path(tmpdir) / f"{safe_client}-GMCS-report.xlsx"
        try:
            path = build_workbook(client_id=client_id, year=year,
                                  out_path=out_path)
        except Exception as e:
            logger.exception("Workbook build failed for client %s", client_id)
            send_internal_alert(
                "Workbook generation failed",
                f"Client: {client_id} ({client_name})\n"
                f"Tenant: {tenant_id} ({tenant_name})\n"
                f"Triggered by: {triggered_by}\nError: {e}",
            )
            return {"ok": False, "client_id": client_id, "error": str(e)}

        first_name = (client_name or tenant_name).split()[0]
        html = (
            f"<p>Hi {first_name},</p>"
            f"<p>Your latest NEPOOL-GIS quarterly generation workbook for "
            f"<b>{client_name}</b> is attached. It covers the last 6 complete "
            f"quarters of generation data we have on file through "
            f"{datetime.utcnow():%B %d, %Y}, one sheet per array.</p>"
            f"<p>Manage your account or change report frequency at "
            f"<a href='https://solaroperator.org/account.html'>"
            f"solaroperator.org/account</a>.</p>"
            f"<p>Questions? Just reply.</p>"
            f"<p>— Solar Operator</p>"
        )
        text = (
            f"Hi {first_name},\n\n"
            f"Your latest NEPOOL-GIS quarterly generation workbook for "
            f"{client_name} is attached (through "
            f"{datetime.utcnow():%B %d, %Y}).\n\n"
            f"Manage your account at https://solaroperator.org/account.html\n\n"
            f"Questions? Just reply.\n\n— Solar Operator"
        )
        # Send to primary; cc the extras
        primary, *cc = recipients
        sent = send_workbook_email(
            to=primary,
            subject=f"{client_name} — NEPOOL-GIS quarterly report",
            html=html,
            text=text,
            workbook_path=str(path),
            filename=f"{safe_client}-GMCS-report.xlsx",
        )
        # If there are CCs, send a copy to each (Resend's SDK doesn't
        # always expose CC; explicit sends are safer for delivery tracking).
        for extra in cc:
            send_workbook_email(
                to=extra,
                subject=f"[copy] {client_name} — NEPOOL-GIS quarterly report",
                html=html, text=text, workbook_path=str(path),
                filename=f"{safe_client}-GMCS-report.xlsx",
            )

    if sent and not override_to:
        with SessionLocal() as db:
            c = db.get(Client, client_id)
            if c:
                c.last_delivery_at = now()
            t = db.get(Tenant, tenant_id)
            if t:
                t.last_delivery_at = now()
            db.commit()

    return {
        "ok": True,
        "client_id": client_id,
        "tenant": tenant_id,
        "email_sent": sent,
        "recipients": recipients,
        "triggered_by": triggered_by,
    }


def deliver_for_tenant(tenant_id: str, *, year: Optional[int] = None,
                       override_to: Optional[str] = None,
                       triggered_by: str = "manual") -> dict:
    """Build & email a workbook for EVERY active client under a tenant.

    Returns an aggregate result with one entry per client. This is the
    function called by:
      - the customer-facing "send report now" button (sends all clients)
      - the scheduler cron
      - the ops admin force-send
    """
    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        if not tenant:
            raise ValueError(f"Tenant not found: {tenant_id}")
        is_active = tenant.active or tenant.subscription_status in (
            "comped", "trialing")
        if not is_active and triggered_by != "ops":
            return {"ok": False, "reason": "tenant not active",
                    "tenant": tenant_id, "results": []}
        clients = db.execute(
            select(Client).where(Client.tenant_id == tenant_id,
                                 Client.active == True)  # noqa: E712
        ).scalars().all()
        client_ids = [c.id for c in clients]

    results = []
    for cid in client_ids:
        try:
            results.append(deliver_for_client(
                cid, year=year, override_to=override_to,
                triggered_by=triggered_by))
        except Exception as e:
            logger.exception("Delivery failed for client %s", cid)
            results.append({"ok": False, "client_id": cid, "error": str(e)})
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "tenant": tenant_id,
        "client_count": len(client_ids),
        "delivered": ok_count,
        "results": results,
        "triggered_by": triggered_by,
    }
