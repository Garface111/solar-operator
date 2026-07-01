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
    print("=== NEPOOL Operator schema migration ===")
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
            ("product",                "ALTER TABLE tenants ADD COLUMN product VARCHAR(32) DEFAULT 'nepool' NOT NULL"),
            ("billing_plan",           "ALTER TABLE tenants ADD COLUMN billing_plan VARCHAR(32)"),
            # Array Operator automatic warranty-claims send policy (Jun 2026)
            ("claim_send_mode",        "ALTER TABLE tenants ADD COLUMN claim_send_mode VARCHAR(16) DEFAULT 'manual' NOT NULL"),
            ("claim_grace_hours",      "ALTER TABLE tenants ADD COLUMN claim_grace_hours INTEGER DEFAULT 24 NOT NULL"),
            # Cross-product sibling link (Jun 2026): one extension install feeds
            # BOTH a user's NEPOOL and Array Operator tenants. Nullable, self-
            # referential, NULL for every existing tenant → no fan-out until a
            # link is deliberately established (api.tenant_link.link_by_email).
            ("linked_tenant_id",       "ALTER TABLE tenants ADD COLUMN linked_tenant_id VARCHAR(32)"),
            # Operator-level BYO generation spreadsheet (Jun 2026). Mirrors the
            # per-subscription tracker columns but keyed to the TENANT. Additive +
            # nullable — NULL for every existing tenant; no auto-append in v1, so
            # these never touch the live billing path. BYTEA/JSON/TIMESTAMP work on
            # both sqlite dev + Postgres prod.
            ("tracker_workbook",       "ALTER TABLE tenants ADD COLUMN tracker_workbook BYTEA"),
            ("tracker_filename",       "ALTER TABLE tenants ADD COLUMN tracker_filename VARCHAR(300)"),
            ("tracker_map",            "ALTER TABLE tenants ADD COLUMN tracker_map JSON"),
            ("tracker_updated_at",     "ALTER TABLE tenants ADD COLUMN tracker_updated_at TIMESTAMP"),
            # Consent / authorization-to-access record (Jun 2026): the Terms/
            # Privacy + account-access authorization version accepted at signup,
            # when, and from what IP — durable proof of consent. Nullable; NULL
            # for pre-existing tenants.
            ("consent_version",        "ALTER TABLE tenants ADD COLUMN consent_version VARCHAR(40)"),
            ("consent_at",             "ALTER TABLE tenants ADD COLUMN consent_at TIMESTAMP"),
            ("consent_ip",             "ALTER TABLE tenants ADD COLUMN consent_ip VARCHAR(64)"),
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
            ("CREATE INDEX IF NOT EXISTS ix_tenants_linked_tenant_id ON tenants (linked_tenant_id)",
             "ix_tenants_linked_tenant_id"),
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

        # arrays.portfolio_name (added 2026-06-30 for the Analysis-tab portfolio
        # hierarchy — operator-assigned group label; nullable, no backfill needed)
        if not column_exists(conn, "arrays", "portfolio_name"):
            conn.execute(text(
                "ALTER TABLE arrays ADD COLUMN portfolio_name VARCHAR(80)"
            ))
            print("  + arrays.portfolio_name")

        # arrays.reminder (added 2026-06-30 for the Analysis-tab O&M "Reminder"
        # column — operator note; nullable, no backfill)
        if not column_exists(conn, "arrays", "reminder"):
            conn.execute(text("ALTER TABLE arrays ADD COLUMN reminder TEXT"))
            print("  + arrays.reminder")

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

        # 2026-06-30 Predicted-vs-actual production: per-array geolocation +
        # geometry, cached after a one-time geocode of the linked
        # UtilityAccount.service_address. All NULLable → existing arrays untouched
        # until their first forecast request lazily fills lat/lng. See
        # api/forecasting.py.
        forecast_cols = [
            ("latitude",         "ALTER TABLE arrays ADD COLUMN latitude DOUBLE PRECISION"),
            ("longitude",        "ALTER TABLE arrays ADD COLUMN longitude DOUBLE PRECISION"),
            ("geocode_source",   "ALTER TABLE arrays ADD COLUMN geocode_source VARCHAR(24)"),
            ("geocoded_address", "ALTER TABLE arrays ADD COLUMN geocoded_address TEXT"),
            ("geocoded_at",      "ALTER TABLE arrays ADD COLUMN geocoded_at TIMESTAMP"),
            ("tilt_deg",         "ALTER TABLE arrays ADD COLUMN tilt_deg DOUBLE PRECISION"),
            ("azimuth_deg",      "ALTER TABLE arrays ADD COLUMN azimuth_deg DOUBLE PRECISION"),
            ("geometry_source",  "ALTER TABLE arrays ADD COLUMN geometry_source VARCHAR(16)"),
        ]
        _fc_added = []
        for col, sql in forecast_cols:
            if not column_exists(conn, "arrays", col):
                conn.execute(text(sql))
                _fc_added.append(col)
        if _fc_added:
            print(f"  + arrays forecast geo/geometry cols: {_fc_added}")

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

        # 2026-06 Array Operator inverter down/underperformance email alerts.
        inverter_alert_cols = [
            ("inverter_alerts_enabled",
             "ALTER TABLE tenants ADD COLUMN inverter_alerts_enabled BOOLEAN NOT NULL DEFAULT FALSE"),
            ("inverter_alert_email",
             "ALTER TABLE tenants ADD COLUMN inverter_alert_email VARCHAR(200)"),
            ("inverter_alert_threshold_pct",
             "ALTER TABLE tenants ADD COLUMN inverter_alert_threshold_pct INTEGER NOT NULL DEFAULT 50"),
            ("inverter_alert_grace_hours",
             "ALTER TABLE tenants ADD COLUMN inverter_alert_grace_hours INTEGER NOT NULL DEFAULT 12"),
        ]
        for col, sql in inverter_alert_cols:
            if not column_exists(conn, "tenants", col):
                conn.execute(text(sql))
                print(f"  + tenants.{col}")

        # 2026-06-24 Inverter alerts are now ON BY DEFAULT (Ford): the fleet watch
        # should page an operator about a down inverter without them first finding a
        # toggle. One-time flip existing tenants false→true and move the column
        # default to true. Gated on the column's CURRENT default so it runs exactly
        # once and never clobbers a later deliberate per-tenant opt-out. (comm_gap
        # false positives from the extension capture cadence are separately
        # suppressed in inverter_alert_sweep, so "on" never means false spam.)
        # information_schema is Postgres-only; the try/except no-ops on sqlite dev,
        # where the model's server_default="true" already covers fresh DBs.
        try:
            cur_default = conn.execute(text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'tenants' "
                "AND column_name = 'inverter_alerts_enabled'"
            )).scalar()
            if cur_default is None or "false" in str(cur_default).lower():
                res = conn.execute(text(
                    "UPDATE tenants SET inverter_alerts_enabled = true "
                    "WHERE inverter_alerts_enabled = false"
                ))
                conn.execute(text(
                    "ALTER TABLE tenants "
                    "ALTER COLUMN inverter_alerts_enabled SET DEFAULT true"
                ))
                n = res.rowcount if res.rowcount is not None else 0
                print(f"  ~ tenants.inverter_alerts_enabled default → true "
                      f"(one-time flip, {n} tenant(s))")
        except Exception as _e:
            print(f"  (inverter_alerts_enabled default flip skipped: {_e})")

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
            # NOTE: no "IF NOT EXISTS" — that's Postgres-only syntax and breaks
            # sqlite dev DBs. The column_exists() guard below already makes
            # this idempotent on both engines.
            ("refresh_failures",
             "ALTER TABLE utility_sessions ADD COLUMN refresh_failures INTEGER NOT NULL DEFAULT 0"),
            ("last_refresh_at",
             "ALTER TABLE utility_sessions ADD COLUMN last_refresh_at TIMESTAMP NULL"),
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

        # 2026-06 residential-customer detection (api/app.py /v1/sync). Column
        # exists in models.py with server_default but had NO migration here —
        # any pre-existing DB (sqlite dev included) broke on first SELECT.
        if not column_exists(conn, "utility_accounts", "is_residential"):
            conn.execute(text(
                "ALTER TABLE utility_accounts ADD COLUMN is_residential "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            print("  + utility_accounts.is_residential")
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

        # 2026-06-06 signoff removal: null out stored email_body_template rows
        # that contain only the OLD default (body + dashboard footer). Any tenant
        # who stored exactly the old default should revert to the new one
        # automatically. Custom templates (different body text) are left alone.
        # Idempotent — rows already NULL are untouched; rows with custom text are
        # untouched.
        OLD_FOOTER_MARKER = "Manage at"
        OLD_FOOTER_MARKER2 = "your dashboard"
        from api.email_templates import DEFAULT_BODY_TEMPLATE as _NEW_DEFAULT
        OLD_DEFAULT_BODY = (
            "<p>Dear {{client_name}},</p>"
            "<p>Here is your quarterly NEPOOL-GIS report from {{period_start}} to"
            " {{period_end}}. Please reach out with any questions.</p>"
            "{{signoff}}"
            "<p style='margin-top:24px;font-size:12px;color:#6b7280;'>"
            "<em>Manage at <a href='{{dashboard_url}}'>your dashboard</a>.</em></p>"
        )
        candidates = conn.execute(text(
            "SELECT id, email_body_template FROM tenants "
            "WHERE email_body_template IS NOT NULL"
        )).fetchall()
        nulled = 0
        for row_id, stored in candidates:
            if stored is None:
                continue
            sl = stored.lower()
            if OLD_FOOTER_MARKER.lower() in sl and OLD_FOOTER_MARKER2.lower() in sl:
                if stored.strip() == OLD_DEFAULT_BODY.strip():
                    conn.execute(text(
                        "UPDATE tenants SET email_body_template = NULL WHERE id = :id"
                    ), {"id": row_id})
                    nulled += 1
                    print(f"  ↪ tenant {row_id}: cleared old dashboard-footer template")
        if nulled == 0:
            print("  ↪ signoff footer cleanup: no tenants had the old default stored")
        else:
            print(f"  ↪ signoff footer cleanup: nulled {nulled} tenant(s)")

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

        # 2026-06-06 all-set event (feat/all-set-event): persist onboarding array
        # estimate so the dashboard can fire a "You're all set!" milestone.
        # NULL for tenants who signed up before this column existed (including Bruce).
        if not column_exists(conn, "tenants", "onboarding_array_estimate"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN onboarding_array_estimate INTEGER"
            ))
            print("  + tenants.onboarding_array_estimate")

        # 2026-06-06 daily_generation: daily kWh per array per calendar day.
        # Table is created by init_db() (create_all) above.
        # Explicit index creation is idempotent via IF NOT EXISTS; covers
        # environments where the table existed before these indexes were defined.
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_daily_gen_tenant_id ON daily_generation (tenant_id)",
            "CREATE INDEX IF NOT EXISTS ix_daily_gen_array_id ON daily_generation (array_id)",
            "CREATE INDEX IF NOT EXISTS ix_daily_gen_day ON daily_generation (day)",
        ]:
            conn.execute(text(idx_sql))
        print("  ✓ daily_generation table + indexes ensured")

        # 2026-06-06 Shared read-only demo tenant (feat/demo-tenant).
        # is_demo flags the single public demo tenant; every real tenant stays
        # False. Mutating endpoints refuse for is_demo tenants.
        if not column_exists(conn, "tenants", "is_demo"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            print("  + tenants.is_demo")
        conn.execute(text(
            "UPDATE tenants SET is_demo = FALSE WHERE is_demo IS NULL"
        ))

        # 2026-06-06 SolarEdge Monitoring API integration (feat/solaredge-adapter).
        # Stores operator-pasted API key (plain text — read-only scope) and the
        # SolarEdge site ID this array maps to. Nullable; NULL means no SolarEdge.
        solaredge_cols = [
            ("solaredge_api_key",
             "ALTER TABLE arrays ADD COLUMN solaredge_api_key TEXT"),
            ("solaredge_site_id",
             "ALTER TABLE arrays ADD COLUMN solaredge_site_id INTEGER"),
        ]
        for col, sql in solaredge_cols:
            if not column_exists(conn, "arrays", col):
                conn.execute(text(sql))
                print(f"  + arrays.{col}")

        # 2026-06-07 Split Tenant.name into operator_name + company_name
        # (feat/split-operator-and-company-name). Bug: the single `name` field
        # was being stamped on email From: AND report signoffs AND internal
        # alerts — telling the operator's clients that their company is named
        # "Ford Genereaux". Now: company_name on From / titles / Stripe;
        # operator_name on signoffs / greetings / alerts.
        #
        # Legacy `tenants.name` column stays for now (deprecated; mirrors
        # company_name on write). Backfill copies name → company_name.
        # operator_name defaults NULL so existing operators see an empty
        # "Your name" row on the Settings card and can fill it in.
        namesplit_cols = [
            ("operator_name",
             "ALTER TABLE tenants ADD COLUMN operator_name VARCHAR(120)"),
            ("company_name",
             "ALTER TABLE tenants ADD COLUMN company_name VARCHAR(200)"),
        ]
        for col, sql in namesplit_cols:
            if not column_exists(conn, "tenants", col):
                conn.execute(text(sql))
                print(f"  + tenants.{col}")
        # Idempotent backfill: only touches rows where company_name is still NULL.
        backfilled = conn.execute(text(
            "UPDATE tenants SET company_name = name "
            "WHERE company_name IS NULL AND name IS NOT NULL"
        )).rowcount
        if backfilled:
            print(f"  ↪ backfilled company_name from name on {backfilled} tenant(s)")

        # 2026-06-07 No-upfront-payment: exactly-once trial-end reminder dedup.
        # trial_reminder_sent_at is stamped when the ~3-day "trial ending, no
        # card" reminder goes out, replacing the fragile now+2d/now+3d rolling
        # window in scheduler.send_trial_ending_reminders. NULL = not yet sent.
        if not column_exists(conn, "tenants", "trial_reminder_sent_at"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN trial_reminder_sent_at TIMESTAMP NULL"
            ))
            print("  + tenants.trial_reminder_sent_at")

        # 2026-06-21 Multi-touch trial reminder: urgent ~2-day "last chance"
        # nudge, stamped separately so the early (7-day) and urgent touches
        # are each exactly-once. NULL = urgent reminder not yet sent.
        if not column_exists(conn, "tenants", "trial_final_reminder_sent_at"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN trial_final_reminder_sent_at TIMESTAMP NULL"
            ))
            print("  + tenants.trial_final_reminder_sent_at")

        # 2026-06-21 GMP reauth-alert cooldown (suppress repeat reauth emails <7d).
        if not column_exists(conn, "tenants", "gmp_reauth_alert_at"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN gmp_reauth_alert_at TIMESTAMP NULL"
            ))
            print("  + tenants.gmp_reauth_alert_at")

        # 2026-06-09 V2 REC-bearing fuels (feat/v2-rec-fuels). Generalizes the
        # array data model from solar-only to any fuel that mints renewable-
        # energy certificates (wind, hydro, anaerobic digester/biogas, storage).
        # The capture → MWh → REC=floor(MWh) → attestation pipeline is identical;
        # this only labels what an array is. fuel_type defaults to 'solar' with a
        # server_default so existing rows and all solar writers/reports/scrapers
        # stay byte-identical. cert_registry is nullable (NULL = the implicit
        # NEPOOL-GIS registry solar has always used).
        recfuel_cols = [
            ("fuel_type",
             "ALTER TABLE arrays ADD COLUMN fuel_type VARCHAR(20) DEFAULT 'solar'"),
            ("cert_registry",
             "ALTER TABLE arrays ADD COLUMN cert_registry VARCHAR(40)"),
        ]
        for col, sql in recfuel_cols:
            if not column_exists(conn, "arrays", col):
                conn.execute(text(sql))
                print(f"  + arrays.{col}")
        # Idempotent backfill: stamp every existing array as solar where the
        # column is still NULL (rows created before the server_default took).
        backfilled = conn.execute(text(
            "UPDATE arrays SET fuel_type = 'solar' WHERE fuel_type IS NULL"
        )).rowcount
        if backfilled:
            print(f"  ↪ backfilled fuel_type='solar' on {backfilled} array(s)")

        # 2026-06-13 V2 fuel on the Client (matches the onboarding wizard, which
        # collects a per-client default fuel). Stored so arrays auto-populated
        # later by /v1/sync inherit the operator's onboarding fuel choice.
        # Defaults to 'solar' so existing clients stay byte-identical.
        if not column_exists(conn, "clients", "default_fuel_type"):
            conn.execute(text(
                "ALTER TABLE clients ADD COLUMN default_fuel_type VARCHAR(20) DEFAULT 'solar'"
            ))
            print("  + clients.default_fuel_type")
        backfilled = conn.execute(text(
            "UPDATE clients SET default_fuel_type = 'solar' WHERE default_fuel_type IS NULL"
        )).rowcount
        if backfilled:
            print(f"  ↪ backfilled default_fuel_type='solar' on {backfilled} client(s)")

        # 2026-06-13 Per-login session persistence. Bind each captured
        # UtilitySession to its login identity (customer_number) so an operator
        # who logs into multiple distinct utility customers (one login per
        # client) keeps EVERY login independently usable for scraping — not just
        # the most recently captured one. Nullable; legacy rows stay NULL and
        # fall back to latest-per-provider until the next capture re-binds them
        # (no backfill — the customer identity isn't reliably recoverable from
        # old rows, and the fallback keeps single-login tenants working).
        if not column_exists(conn, "utility_sessions", "customer_number"):
            conn.execute(text(
                "ALTER TABLE utility_sessions ADD COLUMN customer_number VARCHAR(40)"
            ))
            print("  + utility_sessions.customer_number")

        # 2026-06 Multi-vendor inverter framework (feat/inverter-framework).
        # The inverter_connections table comes free via Base.metadata.create_all
        # (init_db() above) — confirmed: it's a brand-new table, no pre-existing
        # rows to migrate. The legacy Array.solaredge_api_key/solaredge_site_id
        # columns stay (added 2026-06-06 block above) for backward compat; arrays
        # with those set and no inverter_connections row are read as a virtual
        # {vendor: "solaredge"} connection. We only ensure the array_id lookup
        # index idempotently here in case the table predates this index.
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_inverter_connections_array_id "
            "ON inverter_connections (array_id)",
        ]:
            conn.execute(text(idx_sql))
        print("  ✓ inverter_connections table + index ensured")

        # 2026-06-26 Encrypt vendor credentials at rest (SO_CONFIG_KEY).
        # InverterConnection.config is now an EncryptedJSON column whose stored
        # form is TEXT (plaintext JSON in pass-through mode, or a Fernet
        # `SOENC1:`+token ciphertext when a key is set). On Postgres the live
        # column is still native `json`, which would REJECT a non-JSON Fernet
        # token — so widen it to TEXT here, BEFORE any key is provisioned. This
        # is a SAFE no-op for plaintext (valid JSON is valid text) and a
        # prerequisite for the key-gated row encryption.
        #   * Postgres-only: SQLite's JSON is already TEXT-affinity and has no
        #     ALTER COLUMN TYPE.
        #   * Idempotent: only fires while the column still reports a json type.
        #   * Type-only: this does NOT encrypt existing rows. Encrypting rows is
        #     a deliberate, key-gated, dry-run-default step —
        #     run scripts/encrypt_vendor_credentials.py once SO_CONFIG_KEY is set.
        #     (The legacy Array.solaredge_api_key is already TEXT, so no ALTER.)
        if conn.dialect.name == "postgresql":
            cfg_type = next(
                (str(c["type"]).lower()
                 for c in inspect(conn).get_columns("inverter_connections")
                 if c["name"] == "config"),
                "",
            )
            if "json" in cfg_type:
                conn.execute(text(
                    "ALTER TABLE inverter_connections "
                    "ALTER COLUMN config TYPE TEXT USING config::text"
                ))
                print("  ~ inverter_connections.config JSON -> TEXT (encryption-at-rest)")

        # 2026-06-13 Array Operator automatic billing reports
        # (feat/array-operator-reports). The billing_report_subscriptions table
        # is created by init_db() (create_all) above — it's brand new, nothing to
        # migrate. Ensure its indexes idempotently in case the table predated
        # them on an environment that ran create_all before this block existed.
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_billing_report_subscriptions_tenant_id "
            "ON billing_report_subscriptions (tenant_id)",
            "CREATE INDEX IF NOT EXISTS ix_billing_sub_tenant_enabled "
            "ON billing_report_subscriptions (tenant_id, enabled)",
            "CREATE INDEX IF NOT EXISTS ix_billing_report_subscriptions_next_send_at "
            "ON billing_report_subscriptions (next_send_at)",
        ]:
            conn.execute(text(idx_sql))
        print("  ✓ billing_report_subscriptions table + indexes ensured")

        # 2026-06-17 Paul's reporting build: dormant GMP invoice PDF attachment.
        # Additive nullable blob — create_all adds it on fresh DBs, but an
        # EXISTING prod billing_report_subscriptions table won't gain it that way.
        # (No "IF NOT EXISTS" — Postgres-only; column_exists() keeps it idempotent
        # on both sqlite dev and Postgres prod.)
        if not column_exists(conn, "billing_report_subscriptions", "gmp_invoice_pdf"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions ADD COLUMN gmp_invoice_pdf BYTEA"
            ))
            print("  + billing_report_subscriptions.gmp_invoice_pdf")

        # 2026-06-17 Paul's reporting build: per-customer delivery mode. Scheduled
        # periods either DRAFT for the operator's approval (default) or auto-send.
        if not column_exists(conn, "billing_report_subscriptions", "delivery_mode"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN delivery_mode VARCHAR(12) DEFAULT 'approval'"
            ))
            print("  + billing_report_subscriptions.delivery_mode")

        # 2026-06-17 Paul's reporting build: MANUAL customer-input path (no xlsx).
        # The operator types a customer (name, array, allocation %) straight into
        # the Reports tab. allocation_pct stores the typed share (0..1) and
        # array_id ties the manual customer to a specific array so delivery/draft
        # can compute the customer share = allocation_pct × the array's period
        # generation. Both NULL for the workbook-driven path. Idempotent on
        # sqlite dev + Postgres prod via column_exists().
        if not column_exists(conn, "billing_report_subscriptions", "allocation_pct"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN allocation_pct DOUBLE PRECISION"
            ))
            print("  + billing_report_subscriptions.allocation_pct")
        if not column_exists(conn, "billing_report_subscriptions", "array_id"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN array_id INTEGER"
            ))
            print("  + billing_report_subscriptions.array_id")

        # 2026-07-01 Anna/Bruce bill-accuracy check: the offtaker's GMP allocation
        # SHARE of the array's group-excess (0..1), distinct from allocation_pct
        # (the billing multiplier). Used by reconcile's allocation cross-check.
        if not column_exists(conn, "billing_report_subscriptions", "array_share_pct"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN array_share_pct DOUBLE PRECISION"
            ))
            print("  + billing_report_subscriptions.array_share_pct")

        # 2026-06-22 Sequential invoice numbering: operator sets a starting number,
        # Array Operator adds 1 per real send. start = seed entered; next = running
        # counter. NULL on both = legacy period-date invoice number.
        if not column_exists(conn, "billing_report_subscriptions", "invoice_number_start"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN invoice_number_start INTEGER"
            ))
            print("  + billing_report_subscriptions.invoice_number_start")
        if not column_exists(conn, "billing_report_subscriptions", "invoice_number_next"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN invoice_number_next INTEGER"
            ))
            print("  + billing_report_subscriptions.invoice_number_next")

        # 2026-06 Multi-array allocations: an offtaker can own a share of several
        # arrays at once. JSON list of {array_id, allocation_pct}. NULL = legacy
        # single array_id/allocation_pct path. JSON works on both sqlite + PG.
        if not column_exists(conn, "billing_report_subscriptions", "array_allocations"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN array_allocations JSON"
            ))
            print("  + billing_report_subscriptions.array_allocations")

        # 2026-06-26 Bring-your-own generation spreadsheet auto-updater. The
        # operator uploads their existing generation-tracking sheet; we detect its
        # columns and append a new row each month as fresh GMP bills land. Additive
        # nullable columns — create_all adds them on fresh DBs, but an EXISTING
        # prod table needs these ALTERs. Idempotent via column_exists() on both
        # sqlite dev + Postgres prod. (No IF NOT EXISTS — Postgres-only.)
        for _col, _sql in [
            ("tracker_workbook",
             "ALTER TABLE billing_report_subscriptions ADD COLUMN tracker_workbook BYTEA"),
            ("tracker_filename",
             "ALTER TABLE billing_report_subscriptions ADD COLUMN tracker_filename VARCHAR(300)"),
            ("tracker_map",
             "ALTER TABLE billing_report_subscriptions ADD COLUMN tracker_map JSON"),
            ("tracker_updated_at",
             "ALTER TABLE billing_report_subscriptions ADD COLUMN tracker_updated_at TIMESTAMP"),
        ]:
            if not column_exists(conn, "billing_report_subscriptions", _col):
                conn.execute(text(_sql))
                print(f"  + billing_report_subscriptions.{_col}")

        # 2026-06 OFFTAKER ↔ UTILITY BILL binding. Offtaker invoices read ONLY the
        # utility's paper bills (Bill.kwh_generated) for the bound GMP account —
        # never vendor/inverter data. This column ties an offtaker subscription to
        # the specific GMP utility account whose bills are theirs. NULL = legacy
        # array-based subscription. INTEGER FK works on both sqlite + Postgres.
        if not column_exists(conn, "billing_report_subscriptions", "utility_account_id"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN utility_account_id INTEGER"
            ))
            print("  + billing_report_subscriptions.utility_account_id")

        # 2026-06-24 Budget billing: a per-offtaker FIXED final amount the operator
        # enters that overrides the calculated Amount Due (the line items still show;
        # only the total becomes the operator's number).
        if not column_exists(conn, "billing_report_subscriptions", "budget_amount_usd"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN budget_amount_usd DOUBLE PRECISION"
            ))
            print("  + billing_report_subscriptions.budget_amount_usd")

        # 2026-06-16 Live current power for extension-captured inverters.
        # The inverters table came free via create_all, but an EXISTING prod table
        # won't gain new columns from create_all — add them explicitly so the
        # capture path can stamp the portal's live power (Fronius/SMA/Chint), which
        # build_fleet_tree surfaces as the card's "Current kW".
        for col, sql in [
            ("last_power_w",  "ALTER TABLE inverters ADD COLUMN last_power_w DOUBLE PRECISION"),
            ("last_power_at", "ALTER TABLE inverters ADD COLUMN last_power_at TIMESTAMP"),
            # SOURCE's own last-data timestamp (Fronius LastImport, SMA reading ts) —
            # the real freshness signal, distinct from last_power_at (OUR capture time).
            # Lets the fleet flag a source that stopped reporting even while we keep
            # re-scraping its frozen value (the West Chester "producing when stopped" bug).
            ("source_last_data_at", "ALTER TABLE inverters ADD COLUMN source_last_data_at TIMESTAMP"),
        ]:
            if not column_exists(conn, "inverters", col):
                conn.execute(text(sql))
                added.append(col)
                print(f"  + inverters.{col}")

        # 2026-06-29 Owner inverter rename persistence. name_is_custom marks an
        # inverter whose name the OWNER set from the dashboard, so the telemetry
        # sync (discover_and_persist) never clobbers it — same principle as never
        # touching their array_id/position. Additive + NOT NULL DEFAULT false:
        # create_all adds it on fresh DBs; an EXISTING prod inverters table needs
        # this explicit ALTER. Idempotent via column_exists() (no IF NOT EXISTS —
        # Postgres-only). Every existing inverter keeps the vendor name (false).
        if not column_exists(conn, "inverters", "name_is_custom"):
            conn.execute(text(
                "ALTER TABLE inverters ADD COLUMN name_is_custom BOOLEAN "
                "NOT NULL DEFAULT false"
            ))
            conn.execute(text(
                "UPDATE inverters SET name_is_custom = false WHERE name_is_custom IS NULL"
            ))
            added.append("name_is_custom")
            print("  + inverters.name_is_custom")

        # 2026-06-18 DATA SPONGE: full energy-record columns on bills. The bills
        # table predates these; create_all won't add columns to an existing prod
        # table, so add the sponge fields explicitly. JSONB for raw_json so the
        # whole bill is queryable later without a re-pull.
        for col, sql in [
            ("kwh_sent_to_grid",    "ALTER TABLE bills ADD COLUMN kwh_sent_to_grid DOUBLE PRECISION"),
            ("kwh_gross_generated", "ALTER TABLE bills ADD COLUMN kwh_gross_generated DOUBLE PRECISION"),
            ("is_net_metered",      "ALTER TABLE bills ADD COLUMN is_net_metered BOOLEAN"),
            ("total_cost",          "ALTER TABLE bills ADD COLUMN total_cost DOUBLE PRECISION"),
            ("net_credit",          "ALTER TABLE bills ADD COLUMN net_credit DOUBLE PRECISION"),
            ("avg_rate_cents_kwh",  "ALTER TABLE bills ADD COLUMN avg_rate_cents_kwh DOUBLE PRECISION"),
            ("supplier",            "ALTER TABLE bills ADD COLUMN supplier VARCHAR(120)"),
            ("raw_json",            "ALTER TABLE bills ADD COLUMN raw_json JSONB"),
            # 2026-06-22 gross solar credit (EXCESS+SOLCRED) — offtaker billing basis.
            ("solar_credit_usd",    "ALTER TABLE bills ADD COLUMN solar_credit_usd DOUBLE PRECISION"),
        ]:
            if not column_exists(conn, "bills", col):
                conn.execute(text(sql))
                added.append(col)
                print(f"  + bills.{col}")

        # 2026-06-18 Array Operator billing rate ($/kWh). Global default on the
        # tenant + per-customer override on the subscription. create_all adds
        # these on fresh DBs; an existing prod table needs the explicit ALTER.
        if not column_exists(conn, "tenants", "default_billing_rate_per_kwh"):
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN default_billing_rate_per_kwh DOUBLE PRECISION"
            ))
            added.append("default_billing_rate_per_kwh")
            print("  + tenants.default_billing_rate_per_kwh")
        if not column_exists(conn, "billing_report_subscriptions", "rate_per_kwh"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions ADD COLUMN rate_per_kwh DOUBLE PRECISION"
            ))
            added.append("rate_per_kwh")
            print("  + billing_report_subscriptions.rate_per_kwh")

        # 2026-06-18 Auto-attach captured GMP bill PDF (per-customer toggle).
        if not column_exists(conn, "billing_report_subscriptions", "auto_attach_gmp"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN auto_attach_gmp BOOLEAN DEFAULT true NOT NULL"
            ))
            added.append("auto_attach_gmp")
            print("  + billing_report_subscriptions.auto_attach_gmp")
        else:
            # Auto-attach is now ON by default. For installs created under the
            # OLD false default, do a ONE-TIME flip of existing rows + change the
            # column default to true. Gated on the column's current default so it
            # runs exactly once and never clobbers later per-offtaker opt-outs.
            try:
                cur_default = conn.execute(text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'billing_report_subscriptions' "
                    "AND column_name = 'auto_attach_gmp'"
                )).scalar()
                if cur_default is not None and "false" in str(cur_default).lower():
                    conn.execute(text(
                        "UPDATE billing_report_subscriptions SET auto_attach_gmp = true "
                        "WHERE auto_attach_gmp = false"
                    ))
                    conn.execute(text(
                        "ALTER TABLE billing_report_subscriptions "
                        "ALTER COLUMN auto_attach_gmp SET DEFAULT true"
                    ))
                    print("  ~ billing_report_subscriptions.auto_attach_gmp default → true (one-time flip)")
            except Exception as _e:
                print(f"  (auto_attach_gmp default flip skipped: {_e})")

        # 2026-06-24 AO performance summary is now OPT-IN (Ford): off by default so it
        # never auto-attaches to an offtaker invoice unless the operator ticks "Attach
        # Array Operator's summary data" on the draft card. The column already exists
        # (create_all, Python-side default, no DB default). One-time flip existing rows
        # true→false + set the DB default false; gated on the column default so a later
        # per-offtaker opt-in (include_summary=true) survives migrate re-runs.
        if not column_exists(conn, "billing_report_subscriptions", "include_summary"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN include_summary BOOLEAN DEFAULT false NOT NULL"
            ))
            print("  + billing_report_subscriptions.include_summary")
        else:
            try:
                cur_default = conn.execute(text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'billing_report_subscriptions' "
                    "AND column_name = 'include_summary'"
                )).scalar()
                if cur_default is None or "false" not in str(cur_default).lower():
                    conn.execute(text(
                        "UPDATE billing_report_subscriptions SET include_summary = false "
                        "WHERE include_summary = true"
                    ))
                    conn.execute(text(
                        "ALTER TABLE billing_report_subscriptions "
                        "ALTER COLUMN include_summary SET DEFAULT false"
                    ))
                    print("  ~ billing_report_subscriptions.include_summary default → false (one-time flip)")
            except Exception as _e:
                print(f"  (include_summary default flip skipped: {_e})")

        # 2026-06-26 "Come review your next bill" dedup marker. When a new GMP
        # bill lands for an offtaker, api/jobs/new_bill_review emails the OPERATOR
        # a "your next invoice is ready to review" prompt; this column stores the
        # latest bill PERIOD already emailed so each new bill fires exactly once.
        if not column_exists(conn, "billing_report_subscriptions", "review_emailed_period"):
            conn.execute(text(
                "ALTER TABLE billing_report_subscriptions "
                "ADD COLUMN review_emailed_period VARCHAR(20)"
            ))
            added.append("review_emailed_period")
            print("  + billing_report_subscriptions.review_emailed_period")

        # 2026-06-18 Durable bill-PDF bytes (auto-attach GMP bill). pdf_path was
        # ephemeral; persist the actual bytes in-row so the PDF survives redeploys.
        for col, sql in [
            ("pdf_bytes",        "ALTER TABLE bills ADD COLUMN pdf_bytes BYTEA"),
            ("pdf_content_type", "ALTER TABLE bills ADD COLUMN pdf_content_type VARCHAR(80)"),
        ]:
            if not column_exists(conn, "bills", col):
                conn.execute(text(sql))
                added.append(col)
                print(f"  + bills.{col}")

        # 2026-06-18 Discount billing model: net rate − discount (default 10% off).
        for tbl, col, sql in [
            ("tenants", "default_discount_pct",
             "ALTER TABLE tenants ADD COLUMN default_discount_pct DOUBLE PRECISION"),
            ("tenants", "default_net_rate_per_kwh",
             "ALTER TABLE tenants ADD COLUMN default_net_rate_per_kwh DOUBLE PRECISION"),
            ("billing_report_subscriptions", "discount_pct",
             "ALTER TABLE billing_report_subscriptions ADD COLUMN discount_pct DOUBLE PRECISION"),
            ("billing_report_subscriptions", "net_rate_per_kwh",
             "ALTER TABLE billing_report_subscriptions ADD COLUMN net_rate_per_kwh DOUBLE PRECISION"),
        ]:
            if not column_exists(conn, tbl, col):
                conn.execute(text(sql))
                added.append(col)
                print(f"  + {tbl}.{col}")

        # 2026-06-18 RateSchedule table (auto-applied blended rate, derived from
        # captured bills). create_all makes it; verify it landed for the log.
        print(f"  {'✓' if inspect(conn).has_table('rate_schedule') else '✗ MISSING'} table rate_schedule")

        # 2026-06-18 GMP daily-interval DATA SPONGE. Two BRAND-NEW tables
        # (gmp_usage_raw = verbatim CSV sponge, gmp_daily_generation = derived
        # per-day kWh) are created for free by init_db()/create_all above; this
        # block just verifies they landed so the migration log is explicit.
        for tbl in ("gmp_usage_raw", "gmp_daily_generation"):
            exists = inspect(conn).has_table(tbl)
            print(f"  {'✓' if exists else '✗ MISSING'} table {tbl}")

        # 2026-06-19 Self-healing deep-history backfill marker. The nightly pull
        # only reaches ~90 days, so a freshly-connected SolarEdge array showed
        # just the current year in Trends. history_backfilled_at stamps when the
        # one-time full multi-year backfill last succeeded (NULL = pending → the
        # scheduled healer + connect hook fill it in).
        if not column_exists(conn, "inverter_connections", "history_backfilled_at"):
            conn.execute(text(
                "ALTER TABLE inverter_connections ADD COLUMN history_backfilled_at TIMESTAMP"
            ))
            added.append("history_backfilled_at")
            print("  + inverter_connections.history_backfilled_at")

        # 2026-06-23 NEPOOL report digests: per-batch delivery log behind the
        # operator's pre-send review + post-send delivery receipt. The
        # report_deliveries table is created by init_db()/create_all above;
        # ensure its indexes idempotently for environments that predate them.
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS ix_report_deliveries_tenant_pending "
            "ON report_deliveries (tenant_id, receipt_sent_at)",
            "CREATE INDEX IF NOT EXISTS ix_report_deliveries_sent_at "
            "ON report_deliveries (sent_at)",
        ]:
            conn.execute(text(idx_sql))
        print(f"  {'✓' if inspect(conn).has_table('report_deliveries') else '✗ MISSING'} "
              f"table report_deliveries")

        # 2026-06-24 Scrub GARBAGE (epoch / pre-2015) inverters.source_last_data_at — a
        # missing/zero SMA gauge reading serialized as 1970-01-01 and surfaced as
        # "20628 days ago" + a false SOURCE-OFFLINE banner. The read path now guards it
        # (_sane_dt), but a re-capture only SETS the field on a truthy parse and never
        # clears a stale value, so scrub the existing junk once here.
        try:
            _res = conn.execute(text(
                "UPDATE inverters SET source_last_data_at = NULL "
                "WHERE source_last_data_at IS NOT NULL AND source_last_data_at < '2015-01-01'"
            ))
            _n = _res.rowcount if _res.rowcount is not None else 0
            if _n:
                print(f"  ~ inverters.source_last_data_at: nulled {_n} garbage epoch value(s)")
        except Exception as _e:
            print(f"  (source_last_data_at scrub skipped: {_e})")

    print("=== Migration complete ===")


if __name__ == "__main__":
    main()
