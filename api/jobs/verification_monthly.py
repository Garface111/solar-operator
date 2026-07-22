"""Monthly performance verification reports (Array Operator owners).

Runs on the 1st (~13:00 UTC via scheduler): for each active array_operator
tenant with monitoring eligibility, build previous-calendar-month verification
and email the PDF pack.

Mirrors morning_fleet_digest eligibility: product=array_operator, active,
ao_gets_vendor_emails, non-demo, contact_email, verification_reports_enabled.
Read-only aside from the email send. Per-tenant failures never raise out of loop.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant
from ..stripe_helpers import ao_gets_vendor_emails

log = logging.getLogger(__name__)


def run_monthly_verification_reports(
    *,
    only_tenant_id: Optional[str] = None,
    force: bool = False,
    period: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Email previous-month (or `period`) verification packs to AO tenants.

    Returns counts: {sent, skipped, errors, ...}.
    force=True: still respects demo/no-email; may ignore opted_out when set.
    dry_run=True: build snapshots but do not send.
    """
    from ..perf_verification.engine import build_month_verification
    from ..perf_verification.report_pack import send_verification_report

    sent: list[str] = []
    skipped: list[dict] = []
    errors: list[str] = []

    with SessionLocal() as outer:
        q = select(Tenant).where(
            Tenant.active.is_(True),
            Tenant.product == "array_operator",
        )
        if only_tenant_id:
            q = q.where(Tenant.id == only_tenant_id)
        tenants = list(outer.execute(q).scalars().all())
        tenant_ids = [t.id for t in tenants]

    for tid in tenant_ids:
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, tid)
                if tenant is None:
                    skipped.append({"tenant_id": tid, "reason": "not_found"})
                    continue

                if getattr(tenant, "is_demo", False):
                    skipped.append({"tenant_id": tid, "reason": "demo_tenant"})
                    continue

                if (
                    not force
                    and getattr(tenant, "verification_reports_enabled", True) is False
                ):
                    skipped.append({"tenant_id": tid, "reason": "opted_out"})
                    continue

                if not ao_gets_vendor_emails(
                    getattr(tenant, "product", None),
                    getattr(tenant, "billing_plan", None),
                ):
                    skipped.append({"tenant_id": tid, "reason": "invoicing_only"})
                    continue

                to = (getattr(tenant, "contact_email", None) or "").strip()
                if not to:
                    skipped.append({"tenant_id": tid, "reason": "no_contact_email"})
                    continue

                snapshot = build_month_verification(tenant, period=period)

                if dry_run:
                    sent.append(tid)
                    log.info(
                        "verification_monthly: dry_run tenant=%s period=%s available=%s",
                        tid,
                        snapshot.get("period"),
                        snapshot.get("available"),
                    )
                    continue

                result = send_verification_report(tenant, snapshot)

                if result.get("sent"):
                    sent.append(tid)
                    log.info(
                        "verification_monthly: sent tenant=%s period=%s available=%s",
                        tid,
                        snapshot.get("period"),
                        snapshot.get("available"),
                    )
                else:
                    skipped.append({
                        "tenant_id": tid,
                        "reason": result.get("reason") or "not_sent",
                    })
        except Exception as exc:  # one bad fleet must not stall the rest
            errors.append(f"{tid}: {exc}")
            log.warning("verification_monthly: tenant %s failed: %s", tid, exc)

    if errors:
        try:
            from ..notify import send_internal_alert

            send_internal_alert(
                f"Monthly verification reports: {len(errors)} tenant(s) failed",
                "Some verification packs could not be built/sent:\n"
                + "\n".join(errors),
            )
        except Exception:
            log.exception("verification_monthly: internal alert failed")

    summary = {
        "sent": sent,
        "skipped": len(skipped),
        "skipped_detail": skipped,
        "errors": errors,
        "period": period,
        "force": force,
        "dry_run": dry_run,
    }
    log.info(
        "verification_monthly: sent=%d skipped=%d errors=%d period=%s",
        len(sent),
        len(skipped),
        len(errors),
        period,
    )
    return summary
