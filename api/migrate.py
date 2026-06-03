"""
Idempotent migration for the June 2026 schema changes:
 - tenants: add stripe_customer_id, stripe_subscription_id, subscription_status,
            report_frequency, last_pull_at, last_delivery_at
 - tenants: index on contact_email
 - new tables: login_tokens, stripe_events

Run on Railway via: `python -m api.migrate`
Idempotent: safe to run multiple times.
"""
from sqlalchemy import text, inspect
from .db import engine, init_db


def column_exists(conn, table: str, column: str) -> bool:
    insp = inspect(conn)
    cols = [c["name"] for c in insp.get_columns(table)]
    return column in cols


def index_exists(conn, table: str, index: str) -> bool:
    insp = inspect(conn)
    return any(i["name"] == index for i in insp.get_indexes(table))


def main():
    print("=== Solar Operator schema migration ===")
    # Create new tables (login_tokens, stripe_events) via metadata.create_all,
    # which is a no-op for existing tables.
    init_db()
    print("✓ Base.metadata.create_all done (new tables created if missing)")

    with engine.begin() as conn:
        added = []
        # Add columns to tenants
        statements = [
            ("stripe_customer_id",     "ALTER TABLE tenants ADD COLUMN stripe_customer_id VARCHAR(64)"),
            ("stripe_subscription_id", "ALTER TABLE tenants ADD COLUMN stripe_subscription_id VARCHAR(64)"),
            ("subscription_status",    "ALTER TABLE tenants ADD COLUMN subscription_status VARCHAR(32)"),
            ("report_frequency",       "ALTER TABLE tenants ADD COLUMN report_frequency VARCHAR(16) DEFAULT 'monthly'"),
            ("last_pull_at",           "ALTER TABLE tenants ADD COLUMN last_pull_at TIMESTAMP"),
            ("last_delivery_at",       "ALTER TABLE tenants ADD COLUMN last_delivery_at TIMESTAMP"),
        ]
        for col, sql in statements:
            if not column_exists(conn, "tenants", col):
                conn.execute(text(sql))
                added.append(col)
                print(f"  + tenants.{col}")
        # Backfill report_frequency for existing rows
        conn.execute(text(
            "UPDATE tenants SET report_frequency = 'monthly' WHERE report_frequency IS NULL"
        ))
        # Indexes on new columns
        for idx_sql, idx_name in [
            ("CREATE INDEX IF NOT EXISTS ix_tenants_stripe_customer_id ON tenants (stripe_customer_id)",
             "ix_tenants_stripe_customer_id"),
            ("CREATE INDEX IF NOT EXISTS ix_tenants_stripe_subscription_id ON tenants (stripe_subscription_id)",
             "ix_tenants_stripe_subscription_id"),
            ("CREATE INDEX IF NOT EXISTS ix_tenants_contact_email ON tenants (contact_email)",
             "ix_tenants_contact_email"),
        ]:
            conn.execute(text(idx_sql))

        if not added:
            print("✓ All tenant columns already present — no schema changes")
        else:
            print(f"✓ Added {len(added)} columns: {added}")

        # arrays.nepool_gis_id (added 2026-06-03 for GMCS-format reports)
        if not column_exists(conn, "arrays", "nepool_gis_id"):
            conn.execute(text(
                "ALTER TABLE arrays ADD COLUMN nepool_gis_id VARCHAR(20)"
            ))
            print("  + arrays.nepool_gis_id")

    print("=== Migration complete ===")


if __name__ == "__main__":
    main()
