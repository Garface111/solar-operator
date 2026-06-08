"""One-shot: hard-delete every tenant matching bruce.genereaux@gmail.com.

Cascades through all known per-tenant tables. Run with:
    railway ssh "cd /app && python -m scripts.delete_bruce"

Idempotent — re-running after a clean signup just no-ops.
"""
from __future__ import annotations

from sqlalchemy import text

from api.db import SessionLocal

EMAIL = "bruce.genereaux@gmail.com"

# Tables that carry tenant_id but DON'T cascade via ORM relationships.
# Order doesn't matter inside this list — they all FK to tenants.id.
TENANT_SCOPED_TABLES = [
    "bills",
    "jobs",
    "login_tokens",
    "stripe_events",
    "delete_history",
    "client_merge_dismissals",
    "array_merge_dismissals",
    "daily_generation",
    "verification_checks",
    "capture_events",
    "utility_sessions",
    "utility_accounts",
    "arrays",
    "clients",
]


def main() -> None:
    with SessionLocal() as db:
        rows = db.execute(
            text("SELECT id, name, contact_email, active FROM tenants WHERE contact_email = :e"),
            {"e": EMAIL},
        ).fetchall()
        if not rows:
            print(f"No tenants found for {EMAIL}. Nothing to do.")
            return

        print(f"Found {len(rows)} tenant row(s) for {EMAIL}:")
        for r in rows:
            print(f"  id={r.id} name={r.name!r} active={r.active}")

        tenant_ids = [r.id for r in rows]

        for table in TENANT_SCOPED_TABLES:
            res = db.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = ANY(:ids)"),
                {"ids": tenant_ids},
            )
            print(f"  deleted {res.rowcount:>5} row(s) from {table}")

        res = db.execute(
            text("DELETE FROM tenants WHERE id = ANY(:ids)"),
            {"ids": tenant_ids},
        )
        print(f"  deleted {res.rowcount:>5} row(s) from tenants")

        db.commit()
        print("DONE — Bruce can re-signup at solaroperator.org.")


if __name__ == "__main__":
    main()
