"""Operator-run account delete — the /v1/account/delete semantics, by email.

The self-serve endpoint (api/account.delete_account) needs the account holder's
own session token AND their password. When a customer asks US to close their
account, nobody can satisfy that, so this script performs the identical wipe
from the ops side.

Usage (dry run FIRST — this is the default):
    railway ssh "cd /app && python -m scripts.admin_delete_account <email>"
    railway ssh "cd /app && python -m scripts.admin_delete_account <email> --yes"

Flags:
    --yes                 actually write (without it, nothing is committed)
    --detach-recipient    also scrub the address from clients.contact_email /
                          clients.cc_emails (see "recipient" note below)
    --reason "..."        audit string recorded on the vault purge

What it does to a matched TENANT (same order as the endpoint):
    purge Cloud Capture vault + utility session JWTs, cancel the Stripe
    subscription, active=False, subscription_status='deleted', drop the
    subscription/payment-method ids, anonymize contact_email to
    deleted+<tid>@invalid.local, clear gmp_email/gmp_username/password_hash,
    bump the session epoch, and burn unused magic links.

What it deliberately does NOT do: drop the tenants row or any report/bill
history. That is scripts/delete_tenant_by_email.py (irreversible, and it does
not cancel Stripe). Keeping the row is what lets the address re-signup cleanly
and keeps FK children from orphaning.

RECIPIENT NOTE — why this script looks at clients too:
    Deactivating a tenant stops the mail the SCHEDULER sends that tenant
    (every scheduler sweep filters on Tenant.active). It does NOT stop report
    or invoice mail addressed to someone as a CLIENT contact/cc of a DIFFERENT,
    still-active tenant — that fanout reads clients.contact_email and
    clients.cc_emails. An address can be both. The script always reports both
    kinds of match; --detach-recipient is what actually stops the second kind.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from sqlalchemy import func, select

from api.db import SessionLocal
from api.models import Tenant, Client, LoginToken

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("admin_delete_account")

ANON_DOMAIN = "invalid.local"


def _split_ccs(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").replace(";", ",").split(",") if p.strip()]


def find_tenants(db, email: str) -> list[Tenant]:
    """Exact (case-insensitive) contact_email match.

    Deliberately NOT a LIKE/substring match: a substring is fine for a dev
    cleanup script but not for a one-way delete against live customers —
    'ford' would sweep every ford*@ address in the table.
    """
    return list(
        db.execute(
            select(Tenant).where(func.lower(Tenant.contact_email) == email)
        ).scalars().all()
    )


def find_recipient_clients(db, email: str) -> list[tuple[Client, str]]:
    """Clients where this address is the contact or sits in the cc list."""
    hits: list[tuple[Client, str]] = []
    for c in db.execute(select(Client)).scalars().all():
        if (c.contact_email or "").strip().lower() == email:
            hits.append((c, "contact_email"))
        elif email in [x.lower() for x in _split_ccs(c.cc_emails)]:
            hits.append((c, "cc_emails"))
    return hits


def delete_tenant(db, tenant: Tenant, *, reason: str, apply: bool) -> dict:
    """Mirror of api.account.delete_account's write block."""
    from api.vault_lifecycle import purge_tenant_sensitive_data

    tid = tenant.id
    old_email = tenant.contact_email
    counts: dict = {}

    if apply:
        counts = (purge_tenant_sensitive_data(db, tid, reason=reason) or {}).get("counts") or {}
    else:
        log.info("    [dry-run] would purge Cloud Capture vault + utility sessions")

    sub_id = tenant.stripe_subscription_id
    if sub_id:
        if apply and os.getenv("STRIPE_SECRET_KEY"):
            import stripe
            try:
                stripe.Subscription.cancel(sub_id)
                log.info("    stripe: canceled %s", sub_id)
            except Exception:
                # Never block the wipe on Stripe — same posture as the endpoint.
                log.exception("    stripe: cancel FAILED for %s — cancel it by hand", sub_id)
        elif apply:
            log.warning("    stripe: STRIPE_SECRET_KEY unset — %s NOT canceled, do it by hand", sub_id)
        else:
            log.info("    [dry-run] would cancel stripe subscription %s", sub_id)
    else:
        log.info("    stripe: no subscription on record")

    if not apply:
        log.info("    [dry-run] would deactivate + anonymize %s and burn its magic links", old_email)
        return {"tenant_id": tid, "previous_email": old_email, "vault": counts}

    tenant.active = False
    tenant.subscription_status = "deleted"
    tenant.stripe_subscription_id = None
    if hasattr(tenant, "stripe_payment_method_id"):
        tenant.stripe_payment_method_id = None
    tenant.contact_email = f"deleted+{tid[:12]}@{ANON_DOMAIN}"
    if hasattr(tenant, "password_hash"):
        tenant.password_hash = None

    # The GMP identifiers live on the tenant's CLIENT rows, not on Tenant.
    # (api/account.delete_account set tenant.gmp_email/gmp_username, which are
    # not mapped columns on Tenant — SQLAlchemy accepted the assignment as a
    # plain instance attribute and wrote nothing. Fixed there too.) Clearing
    # them matters beyond tidiness: /v1/sync auto-populates arrays onto a client
    # by matching an incoming capture against clients.gmp_email/gmp_username, so
    # a stale identifier can attach fresh utility data to a deleted account.
    cleared = 0
    for c in db.execute(select(Client).where(Client.tenant_id == tid)).scalars().all():
        if c.gmp_email or c.gmp_username:
            c.gmp_email = None
            c.gmp_username = None
            c.gmp_autopopulate = False
            cleared += 1
    log.info("    cleared GMP identifiers on %d client row(s)", cleared)

    from api.account import bump_session_epoch
    bump_session_epoch(db, tenant)

    burned = 0
    for lt in db.execute(
        select(LoginToken).where(
            LoginToken.tenant_id == tid, LoginToken.used_at.is_(None)
        )
    ).scalars().all():
        lt.used_at = datetime.utcnow()
        burned += 1
    log.info("    burned %d outstanding magic link(s)", burned)
    return {"tenant_id": tid, "previous_email": old_email, "vault": counts}


def detach_recipient(db, client: Client, field: str, email: str, *, apply: bool) -> None:
    if field == "contact_email":
        if not apply:
            log.info("    [dry-run] would clear contact_email on client %s (%s)", client.id, client.name)
            return
        client.contact_email = None
    else:
        kept = [x for x in _split_ccs(client.cc_emails) if x.lower() != email]
        if not apply:
            log.info("    [dry-run] would rewrite cc_emails on client %s to %r", client.id, ", ".join(kept))
            return
        client.cc_emails = ", ".join(kept) or None
    log.info("    detached from client %s (%s) via %s", client.id, client.name, field)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("email")
    ap.add_argument("--yes", action="store_true", help="actually write (default is dry run)")
    ap.add_argument("--detach-recipient", action="store_true",
                    help="also scrub the address from client contact/cc lists")
    ap.add_argument("--reason", default="operator_account_delete_request")
    args = ap.parse_args()

    email = args.email.strip().lower()
    if "@" not in email:
        log.error("Pass a full email address (exact match, not a substring).")
        return 2
    apply = args.yes
    log.info("%s %s", "APPLYING delete for" if apply else "DRY RUN for", email)

    with SessionLocal() as db:
        tenants = find_tenants(db, email)
        recipients = find_recipient_clients(db, email)

        if not tenants and not recipients:
            log.info("\nNo tenant and no client recipient matches %s. Nothing to delete.", email)
            log.info("If they are still getting mail, it is not this database — check the "
                     "outreach lists (mc-campaign-state) and Resend's own audience.")
            return 0

        log.info("\nTenant accounts: %d", len(tenants))
        for t in tenants:
            log.info("  %s | %s | active=%s | product=%s | sub=%s | created=%s",
                     t.id, t.name, t.active, t.product, t.subscription_status, t.created_at)

        log.info("Client-recipient rows: %d", len(recipients))
        for c, field in recipients:
            log.info("  client %s (%s) of tenant %s — via %s", c.id, c.name, c.tenant_id, field)

        for t in tenants:
            if t.is_demo:
                # require_not_demo, ported: the shared demo tenant is every
                # visitor's "Try it" session, not a person's account.
                log.error("\nREFUSING %s — that is the shared demo tenant.", t.id)
                return 3

        if recipients and not args.detach_recipient:
            log.warning(
                "\nHEADS UP: %d client row(s) still carry this address. Deactivating a "
                "tenant does NOT stop report/invoice mail sent to them as another "
                "tenant's client. Re-run with --detach-recipient to stop that too.",
                len(recipients),
            )

        results = []
        for t in tenants:
            log.info("\nDeleting tenant %s:", t.id)
            results.append(delete_tenant(db, t, reason=args.reason, apply=apply))

        if args.detach_recipient and recipients:
            log.info("\nDetaching recipient rows:")
            for c, field in recipients:
                detach_recipient(db, c, field, email, apply=apply)

        if not apply:
            db.rollback()
            log.info("\nDRY RUN — nothing written. Re-run with --yes to apply.")
            return 0

        db.commit()

    for r in results:
        try:
            from api.notify import send_internal_alert
            send_internal_alert(
                f"Account deleted (operator): {r['tenant_id']}",
                f"Tenant {r['tenant_id']} (was {r['previous_email']}) deleted by operator "
                f"request. reason={args.reason} vault={r.get('vault')}",
            )
        except Exception:
            log.warning("internal alert failed (delete itself succeeded)")

    log.info("\nDone. Deleted %d tenant account(s).", len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
