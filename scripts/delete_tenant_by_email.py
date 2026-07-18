"""Delete tenants matching an email pattern (case-insensitive substring).

Usage: railway ssh "cd /app && python -m scripts.delete_tenant_by_email <pattern>"
Example: python -m scripts.delete_tenant_by_email ford.genereaux

Hard-deletes Cloud Capture vault rows (portal_credential, harvest_run,
portal_login_status) BEFORE the tenants row so FKs never block.
"""
import sys
from sqlalchemy import text
from api.db import SessionLocal

if len(sys.argv) < 2:
    print("Usage: python -m scripts.delete_tenant_by_email <email_substring>")
    sys.exit(1)

pat = sys.argv[1].lower()

# Child tables deleted per tenant, vault/sensitive first.
_CHILD_DELETES = (
    "harvest_run",
    "portal_credential",
    "portal_login_status",
    "utility_sessions",
    "bills",  # special: via utility_accounts subquery below
    "utility_accounts",
    "login_tokens",
    "arrays",
    "clients",
    "tenant_templates",
    "capture_events",
    "daily_generation",
    "inverter_daily",
    "inverters",
    "billing_report_subscriptions",
    "delete_history",
    "verification_checks",
    "warranty_claims",
)

with SessionLocal() as db:
    bind = db.get_bind()
    rows = db.execute(
        text(
            "SELECT id, contact_email, name, active, created_at FROM tenants "
            "WHERE lower(contact_email) LIKE :p ORDER BY created_at DESC"
        ),
        {"p": f"%{pat}%"},
    ).all()
    if not rows:
        print(f"No tenants matched '{pat}'.")
        sys.exit(0)
    print(f"Matched {len(rows)} tenant(s):")
    for r in rows:
        print(f"  {r.id} | {r.contact_email} | {r.name} | active={r.active} | {r.created_at}")
    ids = [r.id for r in rows]
    with bind.begin() as conn:
        for tid in ids:
            # bills hang off utility_accounts
            conn.execute(
                text(
                    "DELETE FROM bills WHERE account_id IN "
                    "(SELECT id FROM utility_accounts WHERE tenant_id = :t)"
                ),
                {"t": tid},
            )
            for table in _CHILD_DELETES:
                if table == "bills":
                    continue  # already handled
                try:
                    conn.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                        {"t": tid},
                    )
                except Exception as e:
                    # table may not exist on older deploys
                    print(f"  skip {table}: {type(e).__name__}")
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
    print(f"\nDeleted {len(ids)} tenant(s) and dependent rows (incl. portal_credential).")
