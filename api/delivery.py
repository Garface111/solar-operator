"""
NEPOOL Operator — workbook delivery.

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
from typing import Optional

from sqlalchemy import select, func

from .db import SessionLocal
from .models import Tenant, Client, Array, now
from .writers import build_workbook
from .notify import send_workbook_email, send_internal_alert
from .email_templates import build_context, render_email, resolve_from_header

logger = logging.getLogger(__name__)


def _recipients_for_client(client: Client, tenant: Tenant,
                           override_to: Optional[str]) -> list[str]:
    """Resolve who should receive this client's report.
    Order of preference: override > client.contact_email > tenant.contact_email.
    Always dedupes and includes client.cc_emails when present.

    Falling back to tenant.contact_email is only safe when the client genuinely
    represents the operator themselves (e.g. solo-operator pilot pattern). For
    auto-created or partially-set-up clients, the caller should prefer the
    `to_me` send_mode or filter such clients out — otherwise the operator gets
    an extra copy per missing-contact client they never asked for.
    """
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
                       triggered_by: str = "manual",
                       subject_prefix: str = "") -> dict:
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
        # Split-name (Jun 2026): company_name on From: header & body
        # ({{tenant_name}}); operator_name on signoff.
        tenant_name = tenant.company_name or tenant.name
        operator_name = tenant.operator_name or tenant.company_name or tenant.name
        tenant_id = tenant.id
        tenant_email = (tenant.contact_email or "").strip()
        # V2 email customization snapshot (session closes below).
        # Legacy: if no send_mode is set but cc_on_reports was true, treat as to_both.
        _raw_mode = (tenant.send_mode or "").strip()
        if not _raw_mode:
            send_mode = "to_both" if bool(tenant.cc_on_reports) else "to_client"
        else:
            send_mode = _raw_mode
        # Use the tenant's explicitly-set send_from_email if configured; otherwise
        # fall back to their contact_email so reports appear to come FROM them,
        # not from admin@solaroperator.org.  send_workbook_email will retry with
        # the platform default + Reply-To if Resend rejects an unverified domain.
        effective_from_email = tenant.send_from_email or tenant.contact_email
        send_from_name = tenant.send_from_name
        from_header = resolve_from_header(
            effective_from_email, send_from_name, tenant_name)
        subject_template = tenant.email_subject_template
        body_template = tenant.email_body_template
        signoff_template = tenant.email_signoff
        # Count only arrays that actually appear in the workbook (not soft-
        # deleted, not excluded) so the {{arrays_count}} merge tag in the email
        # body matches what the client sees in the attachment.
        arrays_count = db.execute(
            select(func.count()).select_from(Array)
            .where(
                Array.client_id == client_id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
            )
        ).scalar() or 0

    if not is_active and triggered_by != "ops":
        return {"ok": False, "reason": "tenant or client inactive",
                "client_id": client_id, "client_name": client_name,
                "recipient": "", "tenant": tenant_id}

    # Resolve recipients, honoring send_mode. An explicit override_to (ops
    # force-send) always wins and ignores send_mode.
    if override_to:
        recipients = [override_to]
    elif send_mode == "to_me":
        # Tenant wants the workbook themselves to forward under their own name.
        # Client + client CCs are intentionally NOT contacted.
        recipients = [tenant_email] if tenant_email else []
    elif send_mode == "to_both":
        # Client gets their copy; tenant gets a separate email below.
        recipients = _recipients_for_client(client, tenant, override_to)
    else:
        # send_mode == "to_client" — only fan out if the client has its own
        # contact email, UNLESS cc_on_reports is on (legacy solo-operator mode where
        # the tenant IS the client and tenant.contact_email is the correct recipient).
        # Without cc_on_reports, silently routing to tenant.contact_email would spam
        # a NEPOOL-agent operator once per under-configured client.
        if not (client.contact_email or "").strip() and not bool(tenant.cc_on_reports):
            return {"ok": False, "reason": "no recipient email on file",
                    "client_id": client_id, "client_name": client_name,
                    "recipient": "", "tenant": tenant_id}
        recipients = _recipients_for_client(client, tenant, override_to)
    if not recipients:
        return {"ok": False, "reason": "no recipient email on file",
                "client_id": client_id, "client_name": client_name,
                "recipient": "", "tenant": tenant_id}

    safe_client = client_name.replace(" ", "_").replace("/", "-")
    with tempfile.TemporaryDirectory(prefix=f"so-deliver-c{client_id}-") as tmpdir:
        # Neutral filename — "GMCS" is the solar report's name, but a wind/hydro
        # client gets a fuel-correct REC workbook, so don't brand it solar.
        out_path = pathlib.Path(tmpdir) / f"{safe_client}-report.xlsx"
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
            return {"ok": False, "client_id": client_id,
                    "client_name": client_name, "recipient": "",
                    "reason": "report generation failed", "error": str(e)}

        # Render the email from the tenant's templates (or built-in defaults).
        # Merge tags resolve against this client's real name / array count /
        # headline quarter; see api/email_templates.py.
        ctx = build_context(
            client_name=client_name, tenant_name=tenant_name,
            arrays_count=arrays_count, tenant_email=tenant_email,
            signoff_template=signoff_template,
            tenant_signoff_name=send_from_name or operator_name,
        )
        subject, html, text = render_email(
            subject_template=subject_template,
            body_template=body_template, ctx=ctx,
        )
        if subject_prefix:
            subject = f"{subject_prefix}{subject}"
        filename = f"{safe_client}-report.xlsx"
        # Send to primary; cc the extras. from_header carries the tenant's
        # "send as me" address (send_workbook_email falls back to the platform
        # default if Resend rejects an unverified domain).
        primary, *cc = recipients
        sent = send_workbook_email(
            to=primary, subject=subject, html=html, text=text,
            workbook_path=str(path), filename=filename, from_addr=from_header,
        )
        # If there are CCs, send a copy to each (Resend's SDK doesn't
        # always expose CC; explicit sends are safer for delivery tracking).
        for extra in cc:
            send_workbook_email(
                to=extra, subject=f"[copy] {subject}", html=html, text=text,
                workbook_path=str(path), filename=filename, from_addr=from_header,
            )

        # Operator copy: send a separate email to the operator when:
        #   (a) send_mode is "to_both", OR
        #   (b) the legacy cc_on_reports flag is on (additive — works with any send_mode).
        # Skip when the operator address is already in recipients, and on override sends.
        if (tenant_email and not override_to and tenant_email not in recipients
                and (send_mode == "to_both" or bool(tenant.cc_on_reports))):
            send_workbook_email(
                to=tenant_email, subject=f"[copy] {subject}", html=html, text=text,
                workbook_path=str(path), filename=filename, from_addr=from_header,
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
        "client_name": client_name,
        "tenant": tenant_id,
        "email_sent": sent,
        "recipient": recipients[0] if recipients else "",
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
            results.append({"ok": False, "client_id": cid,
                            "client_name": None, "recipient": "",
                            "reason": "unexpected error", "error": str(e)})
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "tenant": tenant_id,
        "client_count": len(client_ids),
        "delivered": ok_count,
        "results": results,
        "triggered_by": triggered_by,
    }
