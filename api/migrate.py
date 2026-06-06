"""
Idempotent migration for the June 2026 schema changes:
 - tenants: add stripe_customer_id, stripe_subscription_id, subscription_status,
            report_frequency, last_pull_at, last_delivery_at
 - tenants: index on contact_email
 - new tables: login_tokens, stripe_events

Run on Railway via: `python -m api.migrate`
Idempotent: safe to run multiple times.
"""
from datetime import datetime, timedelta
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
            ("report_frequency",       "ALTER TABLE tenants ADD COLUMN report_frequency VARCHAR(16) DEFAULT 'quarterly'"),
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
            "UPDATE tenants SET report_frequency = 'quarterly' WHERE report_frequency IS NULL"
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

        # 2026-06-03 "Copy me on every report" tenant preference.
        if not column_exists(conn, "tenants", "cc_on_reports"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN cc_on_reports BOOLEAN DEFAULT FALSE"
            ))
            print("  + tenants.cc_on_reports")
        conn.execute(text(
            "UPDATE tenants SET cc_on_reports = FALSE WHERE cc_on_reports IS NULL"
        ))

        # 2026-06-03 V2 Email customization: per-tenant send-as + templates.
        email_cust_cols = [
            ("send_from_email",
             "ALTER TABLE tenants ADD COLUMN send_from_email VARCHAR(200)"),
            ("send_from_name",
             "ALTER TABLE tenants ADD COLUMN send_from_name VARCHAR(120)"),
            ("email_subject_template",
             "ALTER TABLE tenants ADD COLUMN email_subject_template TEXT"),
            ("email_body_template",
             "ALTER TABLE tenants ADD COLUMN email_body_template TEXT"),
            ("send_mode",
             "ALTER TABLE tenants ADD COLUMN send_mode VARCHAR(20) DEFAULT 'to_client'"),
        ]
        for col, sql in email_cust_cols:
            if not column_exists(conn, "tenants", col):
                conn.execute(text(sql))
                print(f"  + tenants.{col}")
        conn.execute(text(
            "UPDATE tenants SET send_mode = 'to_client' WHERE send_mode IS NULL"
        ))

        # 2026-06-05 Email sign-off: per-tenant custom sign-off block.
        if not column_exists(conn, "tenants", "email_signoff"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN email_signoff TEXT"
            ))
            print("  + tenants.email_signoff")

        # 2026-06 W2-6: per-client email delivery health (Resend webhook).
        delivery_health_cols = [
            ("last_delivered_at",
             "ALTER TABLE clients ADD COLUMN last_delivered_at TIMESTAMP"),
            ("last_bounced_at",
             "ALTER TABLE clients ADD COLUMN last_bounced_at TIMESTAMP"),
            ("last_bounce_reason",
             "ALTER TABLE clients ADD COLUMN last_bounce_reason TEXT"),
        ]
        for col, sql in delivery_health_cols:
            if not column_exists(conn, "clients", col):
                conn.execute(text(sql))
                print(f"  + clients.{col}")

        # Indexes: onboarding_token lookup + (tenant_id, gmp_email) match in /v1/sync
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_tenants_onboarding_token ON tenants (onboarding_token)",
            "CREATE INDEX IF NOT EXISTS ix_clients_gmp_email ON clients (gmp_email)",
            "CREATE INDEX IF NOT EXISTS ix_clients_gmp_username ON clients (gmp_username)",
            "CREATE INDEX IF NOT EXISTS ix_clients_tenant_gmp_email ON clients (tenant_id, gmp_email)",
        ]:
            conn.execute(text(idx_sql))

        # Soft-delete columns (bulk-delete + undo feature)
        soft_delete_cols = [
            ("clients", "deleted_at",
             "ALTER TABLE clients ADD COLUMN deleted_at TIMESTAMP"),
            ("arrays", "deleted_at",
             "ALTER TABLE arrays ADD COLUMN deleted_at TIMESTAMP"),
            ("utility_accounts", "deleted_at",
             "ALTER TABLE utility_accounts ADD COLUMN deleted_at TIMESTAMP"),
        ]
        for table, col, sql in soft_delete_cols:
            if not column_exists(conn, table, col):
                conn.execute(text(sql))
                print(f"  + {table}.{col}")
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_clients_deleted_at ON clients (deleted_at)",
            "CREATE INDEX IF NOT EXISTS ix_arrays_deleted_at ON arrays (deleted_at)",
            "CREATE INDEX IF NOT EXISTS ix_utility_accounts_deleted_at ON utility_accounts (deleted_at)",
        ]:
            conn.execute(text(idx_sql))

        # C3: trust-this-device toggle — persist_session on login_tokens.
        if not column_exists(conn, "login_tokens", "persist_session"):
            conn.execute(text(
                "ALTER TABLE login_tokens ADD COLUMN persist_session BOOLEAN DEFAULT TRUE"
            ))
            conn.execute(text(
                "UPDATE login_tokens SET persist_session = TRUE WHERE persist_session IS NULL"
            ))
            print("  + login_tokens.persist_session")

        # W3-19: extension heartbeat timestamp on Tenant.
        if not column_exists(conn, "tenants", "extension_heartbeat_at"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN extension_heartbeat_at TIMESTAMP"
            ))
            print("  + tenants.extension_heartbeat_at")

        # 2026-06-04: arrays.excluded — hide below-REC-threshold arrays from
        # reports and billing (data still flows; operator can toggle per array).
        if not column_exists(conn, "arrays", "excluded"):
            conn.execute(text(
                "ALTER TABLE arrays ADD COLUMN excluded BOOLEAN DEFAULT FALSE"
            ))
            conn.execute(text(
                "UPDATE arrays SET excluded = FALSE WHERE excluded IS NULL"
            ))
            print("  + arrays.excluded")

        # 2026-06-04 VEC auto-populate: mirror of GMP triple for VEC provider.
        vec_cols = [
            ("clients", "vec_email",
             "ALTER TABLE clients ADD COLUMN vec_email VARCHAR(200)"),
            ("clients", "vec_username",
             "ALTER TABLE clients ADD COLUMN vec_username VARCHAR(120)"),
            ("clients", "vec_autopopulate",
             "ALTER TABLE clients ADD COLUMN vec_autopopulate BOOLEAN DEFAULT FALSE"),
            ("clients", "vec_last_sync_at",
             "ALTER TABLE clients ADD COLUMN vec_last_sync_at TIMESTAMP"),
        ]
        for table, col, sql in vec_cols:
            if not column_exists(conn, table, col):
                conn.execute(text(sql))
                print(f"  + {table}.{col}")
        conn.execute(text(
            "UPDATE clients SET vec_autopopulate = FALSE WHERE vec_autopopulate IS NULL"
        ))

        # 2026-06-04: clients.is_placeholder — seed-flag for the "your first
        # client" row dropped in by the array-count-only onboarding path.
        # Cleared the moment the operator renames the client or the extension
        # auto-populates real arrays into it.
        if not column_exists(conn, "clients", "is_placeholder"):
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN is_placeholder BOOLEAN DEFAULT FALSE"
            ))
            conn.execute(text(
                "UPDATE clients SET is_placeholder = FALSE WHERE is_placeholder IS NULL"
            ))
            print("  + clients.is_placeholder")
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_clients_vec_email ON clients (vec_email)",
            "CREATE INDEX IF NOT EXISTS ix_clients_vec_username ON clients (vec_username)",
            "CREATE INDEX IF NOT EXISTS ix_clients_tenant_vec_email ON clients (tenant_id, vec_email)",
        ]:
            conn.execute(text(idx_sql))

        # 2026-06-04 Deferred billing: trial columns on tenants.
        deferred_billing_cols = [
            ("trial_ends_at",
             "ALTER TABLE tenants ADD COLUMN trial_ends_at TIMESTAMP NULL"),
            ("stripe_payment_method_id",
             "ALTER TABLE tenants ADD COLUMN stripe_payment_method_id TEXT NULL"),
            ("trial_extended",
             "ALTER TABLE tenants ADD COLUMN trial_extended BOOLEAN NOT NULL DEFAULT FALSE"),
        ]
        for col, sql in deferred_billing_cols:
            if not column_exists(conn, "tenants", col):
                conn.execute(text(sql))
                print(f"  + tenants.{col}")
        conn.execute(text(
            "UPDATE tenants SET trial_extended = FALSE WHERE trial_extended IS NULL"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_tenants_stripe_payment_method_id "
            "ON tenants (stripe_payment_method_id)"
        ))

        # 2026-06-03 V1: quarterly is now the operator default. Flip RECENT test
        # signups (last 7 days) that still carry the old 'monthly' engineer-default
        # over to 'quarterly'. We deliberately bound this to created_at > now-7d so
        # we DON'T touch older real users who may have intentionally chosen monthly.
        res = conn.execute(text(
            "UPDATE tenants SET report_frequency = 'quarterly' "
            "WHERE report_frequency = 'monthly' "
            "AND created_at > :cutoff"
        ), {"cutoff": datetime.utcnow() - timedelta(days=7)})
        flipped = res.rowcount if res.rowcount is not None else 0
        print(f"  ↪ V1 quarterly-default backfill: {flipped} recent tenant(s) monthly→quarterly")

        # 2026-06-05 GMP token refresh worker: failure counter + last-refreshed timestamp.
        for col, sql in [
            ("refresh_failures",
             "ALTER TABLE utility_sessions ADD COLUMN IF NOT EXISTS refresh_failures INTEGER NOT NULL DEFAULT 0"),
            ("last_refresh_at",
             "ALTER TABLE utility_sessions ADD COLUMN IF NOT EXISTS last_refresh_at TIMESTAMP NULL"),
        ]:
            if not column_exists(conn, "utility_sessions", col):
                conn.execute(text(sql))
                print(f"  + utility_sessions.{col}")

        # 2026-06-05 Sandbox canvas v1: persisted node positions.
        # Nullable float pair = not yet placed (auto-arranged on first visit).
        canvas_cols = [
            ("clients", "canvas_x",
             "ALTER TABLE clients ADD COLUMN canvas_x FLOAT"),
            ("clients", "canvas_y",
             "ALTER TABLE clients ADD COLUMN canvas_y FLOAT"),
            ("clients", "canvas_pinned",
             "ALTER TABLE clients ADD COLUMN canvas_pinned BOOLEAN NOT NULL DEFAULT FALSE"),
            ("utility_accounts", "canvas_x",
             "ALTER TABLE utility_accounts ADD COLUMN canvas_x FLOAT"),
            ("utility_accounts", "canvas_y",
             "ALTER TABLE utility_accounts ADD COLUMN canvas_y FLOAT"),
            ("utility_accounts", "canvas_pinned",
             "ALTER TABLE utility_accounts ADD COLUMN canvas_pinned BOOLEAN NOT NULL DEFAULT FALSE"),
            ("utility_accounts", "login_origin_client_id",
             "ALTER TABLE utility_accounts ADD COLUMN login_origin_client_id INTEGER REFERENCES clients(id)"),
        ]
        for table, col, sql in canvas_cols:
            if not column_exists(conn, table, col):
                conn.execute(text(sql))
                print(f"  + {table}.{col}")
        conn.execute(text(
            "UPDATE clients SET canvas_pinned = FALSE WHERE canvas_pinned IS NULL"
        ))
        conn.execute(text(
            "UPDATE utility_accounts SET canvas_pinned = FALSE WHERE canvas_pinned IS NULL"
        ))

        # 2026-06-05 Array-level drag (feat/array-drag): reassignment audit timestamp.
        # Set server-side when /v1/sandbox/array/reassign is called so the canvas
        # can show a "Moved just now" badge for ~10s after the move.
        if not column_exists(conn, "arrays", "reassigned_at"):
            conn.execute(text(
                "ALTER TABLE arrays ADD COLUMN reassigned_at TIMESTAMP"
            ))
            print("  + arrays.reassigned_at")

        # 2026-06-05 Identity + Master account (feat/identity-and-master-account):
        # captured_client_name on utility_accounts stores the original autopop
        # client-name guess so re-capture can respect operator edits; name_edited_at
        # on clients records when the operator last manually changed the name.
        if not column_exists(conn, "utility_accounts", "captured_client_name"):
            conn.execute(text(
                "ALTER TABLE utility_accounts ADD COLUMN captured_client_name VARCHAR(200)"
            ))
            print("  + utility_accounts.captured_client_name")
        if not column_exists(conn, "clients", "name_edited_at"):
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN name_edited_at TIMESTAMP"
            ))
            print("  + clients.name_edited_at")

        # 2026-06-06 freq-cleanup: backfill null → quarterly on clients
        # No-op after first run (API coerces on write, so no new nulls appear).
        n = conn.execute(text(
            "SELECT COUNT(*) FROM clients WHERE report_frequency IS NULL"
        )).scalar()
        if n and n > 0:
            conn.execute(text(
                "UPDATE clients SET report_frequency = 'quarterly' "
                "WHERE report_frequency IS NULL"
            ))
            print(f"  backfilled report_frequency=quarterly on {n} clients")

        # 2026-06-05 Password auth (feat/auth-and-reports): bcrypt hash for
        # operator-set passwords. Magic-link stays as fallback / first-time path.
        if not column_exists(conn, "tenants", "password_hash"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN password_hash VARCHAR(200)"
            ))
            print("  + tenants.password_hash")

        # 2026-06-05 Capture timeline (feat/capture-timeline-devpanel).
        # The capture_events table is created by init_db() (create_all) above.
        # Add composite index explicitly in case the table existed before this
        # migration ran (idempotent via IF NOT EXISTS).
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_capture_events_tenant_id ON capture_events (tenant_id)",
            "CREATE INDEX IF NOT EXISTS ix_capture_events_capture_id ON capture_events (capture_id)",
            "CREATE INDEX IF NOT EXISTS ix_capture_events_created_at ON capture_events (created_at)",
            "CREATE INDEX IF NOT EXISTS ix_capture_events_tenant_created ON capture_events (tenant_id, created_at)",
        ]:
            conn.execute(text(idx_sql))
        print("  ✓ capture_events indexes ensured")

    print("=== Migration complete ===")


if __name__ == "__main__":
    main()
