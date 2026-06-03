"""
Idempotent migration for the June 2026 schema changes:
 - tenants: add stripe_customer_id, stripe_subscription_id, subscription_status,
            report_frequency, last_pull_at, last_delivery_at
 - tenants: index on contact_email
 - new tables: login_tokens, stripe_events

Run on Railway via: `python -m api.migrate`
Idempotent: safe to run multiple times.
"""
from datetime import datetime
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

        # 2026-06-03 Phase-1 expansion: Client layer
        # Idempotency: create_all() above already created `clients` table
        # via Base.metadata, so we only need to (a) add arrays.client_id and
        # (b) backfill a default "Self" Client per existing tenant.
        if not column_exists(conn, "arrays", "client_id"):
            conn.execute(text(
                "ALTER TABLE arrays ADD COLUMN client_id INTEGER REFERENCES clients(id)"
            ))
            print("  + arrays.client_id")
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_arrays_client_id ON arrays (client_id)"
        ))

        # Backfill: every tenant that has at least one array gets a default
        # "Self" Client; every array is linked to its tenant's default client
        # if it currently has client_id IS NULL.
        tenants_to_backfill = [
            r[0] for r in conn.execute(text(
                "SELECT DISTINCT a.tenant_id FROM arrays a "
                "WHERE a.client_id IS NULL"
            )).fetchall()
        ]
        for tid in tenants_to_backfill:
            # Look up tenant name + contact for sensible Client defaults
            row = conn.execute(text(
                "SELECT name, contact_email FROM tenants WHERE id = :tid"
            ), {"tid": tid}).fetchone()
            if row is None:
                continue
            t_name, t_email = row
            # Reuse existing default Client if migration is being re-run
            existing = conn.execute(text(
                "SELECT id FROM clients WHERE tenant_id = :tid AND name = :name"
            ), {"tid": tid, "name": t_name}).fetchone()
            if existing:
                cid = existing[0]
            else:
                conn.execute(text(
                    "INSERT INTO clients (tenant_id, name, contact_email, active, created_at) "
                    "VALUES (:tid, :name, :email, :active, :ts)"
                ), {"tid": tid, "name": t_name, "email": t_email,
                    "active": True, "ts": datetime.utcnow()})
                cid = conn.execute(text(
                    "SELECT id FROM clients WHERE tenant_id = :tid AND name = :name"
                ), {"tid": tid, "name": t_name}).fetchone()[0]
            conn.execute(text(
                "UPDATE arrays SET client_id = :cid "
                "WHERE tenant_id = :tid AND client_id IS NULL"
            ), {"cid": cid, "tid": tid})
            print(f"  ↪ tenant {tid}: linked arrays to default Client id={cid} ('{t_name}')")

        # 2026-06-03 Onboarding wizard: tenant onboarding state + client GMP autopop
        # Idempotent ALTER TABLE for both tables, plus their indexes.
        onboarding_cols = [
            ("tenants", "onboarding_token",
             "ALTER TABLE tenants ADD COLUMN onboarding_token VARCHAR(64)"),
            ("tenants", "onboarding_stage",
             "ALTER TABLE tenants ADD COLUMN onboarding_stage VARCHAR(20) DEFAULT 'pending_payment'"),
            ("clients", "gmp_email",
             "ALTER TABLE clients ADD COLUMN gmp_email VARCHAR(200)"),
            ("clients", "gmp_username",
             "ALTER TABLE clients ADD COLUMN gmp_username VARCHAR(120)"),
            ("clients", "gmp_autopopulate",
             "ALTER TABLE clients ADD COLUMN gmp_autopopulate BOOLEAN DEFAULT FALSE"),
            ("clients", "gmp_last_sync_at",
             "ALTER TABLE clients ADD COLUMN gmp_last_sync_at TIMESTAMP"),
        ]
        for table, col, sql in onboarding_cols:
            if not column_exists(conn, table, col):
                conn.execute(text(sql))
                print(f"  + {table}.{col}")

        # Backfill defaults for existing rows so the columns are never NULL
        # where the model declares a default.
        conn.execute(text(
            "UPDATE tenants SET onboarding_stage = 'pending_payment' "
            "WHERE onboarding_stage IS NULL"
        ))
        conn.execute(text(
            "UPDATE clients SET gmp_autopopulate = FALSE WHERE gmp_autopopulate IS NULL"
        ))

        # Indexes: onboarding_token lookup + (tenant_id, gmp_email) match in /v1/sync
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_tenants_onboarding_token ON tenants (onboarding_token)",
            "CREATE INDEX IF NOT EXISTS ix_clients_gmp_email ON clients (gmp_email)",
            "CREATE INDEX IF NOT EXISTS ix_clients_gmp_username ON clients (gmp_username)",
            "CREATE INDEX IF NOT EXISTS ix_clients_tenant_gmp_email ON clients (tenant_id, gmp_email)",
        ]:
            conn.execute(text(idx_sql))

    print("=== Migration complete ===")


if __name__ == "__main__":
    main()
