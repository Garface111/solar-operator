"""
Quarterly dry-run: build a real workbook for every client under a tenant,
email each one to the TENANT'S OWN address with a [DRY RUN] subject prefix
and a banner paragraph at the top of the body explaining what they're seeing.

Usage (run from repo root with venv active):
  python -m scripts.quarterly_dry_run [--tenant-id <id>] [--to <email>]

  --tenant-id   Target tenant. Defaults to Bruce's tenant:
                ten_14b76982523a3b47
  --to          Override recipient. Defaults to the tenant's contact_email.
                Useful for sending the preview to a colleague or staging inbox.

The dry-run does NOT:
  - Update last_delivery_at on any client or tenant
  - Count against any quota
  - Send anything to real clients

It DOES:
  - Build the actual workbook from live DB data (production bills, real MWh)
  - Render the real email template (subject + body) with the tenant's settings
  - Attach the real .xlsx to the email so the tenant can open it

After running you should verify in your inbox:
  1. Subject line starts with [DRY RUN]
  2. Body has the banner paragraph explaining this is a dry run
  3. Attachment opens cleanly in Excel
  4. Sheet count / data looks right for the current quarter
"""
from __future__ import annotations
import argparse
import logging
import pathlib
import tempfile
import sys
import os

# Ensure the package root is on sys.path so `python -m scripts.quarterly_dry_run`
# works from the repo root without needing to `pip install -e .`.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BRUCE_TENANT_ID = "ten_14b76982523a3b47"

DRY_RUN_BANNER = (
    "<div style='background:#FEF9C3;border:1px solid #EAB308;border-radius:8px;"
    "padding:12px 16px;margin-bottom:20px;font-size:13px;color:#713F12;'>"
    "<strong>This is a dry run.</strong> The workbook attached is a real preview"
    " of what your clients will receive on the next quarterly delivery. No client"
    " has been contacted. Check the attachment, then hit Reply if anything looks off."
    "</div>"
)


def run_dry_run(tenant_id: str, override_to: str | None = None) -> None:
    from api.db import SessionLocal
    from api.models import Tenant, Client, Array
    from api.writers import build_workbook
    from api.notify import send_workbook_email, send_internal_alert
    from api.email_templates import build_context, render_email, resolve_from_header
    from sqlalchemy import select, func

    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        if not tenant:
            logger.error("Tenant not found: %s", tenant_id)
            sys.exit(1)

        to_email = override_to or (tenant.contact_email or "").strip()
        if not to_email:
            logger.error("No recipient email. Pass --to <email> or set tenant contact_email.")
            sys.exit(1)

        clients = db.execute(
            select(Client)
            .where(Client.tenant_id == tenant_id, Client.active == True)  # noqa: E712
            .order_by(Client.name.asc())
        ).scalars().all()

        if not clients:
            logger.error("No active clients for tenant %s", tenant_id)
            sys.exit(1)

        logger.info("Dry run for tenant: %s (%s)", tenant.name, tenant_id)
        logger.info("Sending to: %s", to_email)
        logger.info("Clients: %d", len(clients))

        tenant_name = tenant.name
        tenant_email = (tenant.contact_email or "").strip()
        from_header = resolve_from_header(
            tenant.send_from_email, tenant.send_from_name, tenant_name)
        subject_template = tenant.email_subject_template
        body_template = tenant.email_body_template
        client_data = [
            (c.id, c.name,
             db.execute(
                 select(func.count()).select_from(Array).where(Array.client_id == c.id)
             ).scalar() or 0)
            for c in clients
        ]

    results = []
    for client_id, client_name, arrays_count in client_data:
        safe = client_name.replace(" ", "_").replace("/", "-")
        with tempfile.TemporaryDirectory(prefix=f"so-dryrun-c{client_id}-") as tmpdir:
            out_path = pathlib.Path(tmpdir) / f"{safe}-GMCS-report.xlsx"
            try:
                path = build_workbook(client_id=client_id, out_path=out_path)
            except Exception as e:
                logger.error("Workbook build failed for %s: %s", client_name, e)
                results.append({"client": client_name, "ok": False, "error": str(e)})
                continue

            ctx = build_context(
                client_name=client_name,
                tenant_name=tenant_name,
                arrays_count=arrays_count,
                tenant_email=tenant_email,
            )
            subject, html, _text = render_email(
                subject_template=subject_template,
                body_template=body_template,
                ctx=ctx,
            )
            # Prepend the dry-run banner inside the body
            html_with_banner = DRY_RUN_BANNER + html
            text_plain = (
                "[DRY RUN] This is a preview — no client has been contacted.\n\n"
                + _text
            )

            sent = send_workbook_email(
                to=to_email,
                subject=f"[DRY RUN] {subject}",
                html=html_with_banner,
                text=text_plain,
                workbook_path=str(path),
                filename=f"{safe}-GMCS-report.xlsx",
                from_addr=from_header,
            )
            results.append({"client": client_name, "ok": sent})
            logger.info("  %-30s → %s", client_name, "sent" if sent else "FAILED")

    ok = sum(1 for r in results if r["ok"])
    total = len(results)
    logger.info("Done: %d / %d sent to %s", ok, total, to_email)
    if ok < total:
        logger.warning("Some workbooks failed — check errors above.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant-id", default=BRUCE_TENANT_ID,
                        help=f"Tenant ID (default: {BRUCE_TENANT_ID})")
    parser.add_argument("--to", metavar="EMAIL",
                        help="Override recipient email (default: tenant.contact_email)")
    args = parser.parse_args()
    run_dry_run(args.tenant_id, override_to=args.to)


if __name__ == "__main__":
    main()
