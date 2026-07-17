#!/usr/bin/env python3
"""One-shot: copy a single tenant's subtree from the PROD Postgres to the STAGING
Postgres (Postgres-YSP8) so preprod mirrors Ford's real account. Read-only on prod.

Connections come from /tmp/prod_db_url and /tmp/stg_db_url (Railway public proxy
URLs). Idempotent: wipes the tenant's rows in staging first, then reloads.

FK enforcement is disabled on the dest during load (session_replication_role=
replica), so insert order is irrelevant and refs to prod-only global rows don't
block. The only cross-tenant FK we care about (inverters->inverter_connections)
is satisfied by copying those referenced rows too.

Skipped tables: vendor credential/session state (encrypted with prod's
SO_CONFIG_KEY — useless under staging's different key, and capture is off),
auth tokens, Stripe events, and Sovereign artifacts (Sovereign is off in staging).

TID is a controlled constant inlined into COPY (SELECT ... WHERE ...) — copy_expert
cannot bind params. Do not pass untrusted input.
"""
import io, sys, psycopg2

TID = sys.argv[1] if len(sys.argv) > 1 else "ten_ford_demo_100"
assert all(ch.isalnum() or ch == "_" for ch in TID), "unsafe tenant id"
PROD = open("/tmp/prod_db_url").read().strip()
STG = open("/tmp/stg_db_url").read().strip()

SKIP = {
    "portal_credential", "portal_login_status", "utility_sessions", "sma_consents",
    "login_tokens", "stripe_events",
    "ea_sovereign_desk_messages", "ea_sovereign_message_outbox",
    "ea_sovereign_desk_assets", "ea_sovereign_bridge_tasks",
}


def cols(cur, table):
    cur.execute("""select column_name from information_schema.columns
                   where table_schema='public' and table_name=%s
                   order by ordinal_position""", (table,))
    return [r[0] for r in cur.fetchall()]


def main():
    prod = psycopg2.connect(PROD); pcur = prod.cursor()
    stg = psycopg2.connect(STG); scur = stg.cursor()
    stg.autocommit = False
    scur.execute("set session_replication_role = replica")

    pcur.execute("""select table_name from information_schema.columns
                    where column_name='tenant_id' and table_schema='public'""")
    prod_tenant_tables = set(r[0] for r in pcur.fetchall())
    # only tables that also EXIST in staging (its schema can lag prod)
    scur.execute("""select table_name from information_schema.tables
                    where table_schema='public' and table_type='BASE TABLE'""")
    stg_tables = set(r[0] for r in scur.fetchall())
    tenant_tables = sorted((prod_tenant_tables & stg_tables) - SKIP)
    missing = sorted(prod_tenant_tables - stg_tables - SKIP)
    if missing:
        print(f"[copy] NOTE: prod tenant tables absent in staging (skipped): {missing}")

    def copy_table(table, where):
        common = [c for c in cols(scur, table) if c in set(cols(pcur, table))]
        if not common:
            print(f"[copy] {table}: no common columns, skip"); return 0
        cl = ",".join('"%s"' % c for c in common)
        buf = io.StringIO()
        pcur.copy_expert(f"COPY (SELECT {cl} FROM {table} WHERE {where}) TO STDOUT", buf)
        buf.seek(0)
        scur.copy_expert(f"COPY {table} ({cl}) FROM STDIN", buf)
        return scur.rowcount

    # 1. wipe — FULL, not just this tenant. Staging is a MIRROR of ONE prod
    # tenant, and copied rows keep their PROD primary keys. Any other tenant's
    # rows (notably the demo tenant migrate re-seeds on every boot) draw from
    # the SAME serial id space, so a later refresh collides
    # (duplicate key "ea_messages_pkey"). Emptying first makes the refresh
    # genuinely idempotent and collision-free. FK triggers are off (replica).
    for t in tenant_tables:
        scur.execute(f"delete from {t}")
    scur.execute("delete from tenants")
    print("[copy] wiped staging tenant tables (mirror semantics)")

    # 2. tenant row
    print("[copy] tenants: %s" % copy_table("tenants", "id='%s'" % TID))

    # 3. inverter_connections referenced by this tenant's inverters (global table)
    ic_common = [c for c in cols(scur, "inverter_connections")
                 if c in set(cols(pcur, "inverter_connections"))]
    cl = ",".join('"%s"' % c for c in ic_common)
    buf = io.StringIO()
    pcur.copy_expert(
        f"COPY (SELECT {cl} FROM inverter_connections WHERE id IN "
        f"(SELECT DISTINCT source_connection_id FROM inverters "
        f"WHERE tenant_id='{TID}' AND source_connection_id IS NOT NULL)) TO STDOUT", buf)
    buf.seek(0)
    scur.execute("create temp table _ic (like inverter_connections including defaults) on commit drop")
    scur.copy_expert(f"COPY _ic ({cl}) FROM STDIN", buf)
    scur.execute(f"insert into inverter_connections ({cl}) select {cl} from _ic on conflict (id) do nothing")
    print(f"[copy] inverter_connections (referenced): {scur.rowcount}")

    # 4. all tenant tables
    total = 0
    for t in tenant_tables:
        n = copy_table(t, f"tenant_id='{TID}'")
        total += n or 0
        if n:
            print(f"[copy] {t}: {n}")

    # 5. reset sequences so future staging inserts don't collide with copied ids
    for t in ["tenants", "inverter_connections", *tenant_tables]:
        scur.execute("""select column_name from information_schema.columns
                        where table_schema='public' and table_name=%s
                        and column_default like 'nextval%%'""", (t,))
        for (col,) in scur.fetchall():
            scur.execute(
                f'select setval(pg_get_serial_sequence(%s,%s), coalesce((select max("{col}") from {t}),1))',
                (t, col))

    scur.execute("set session_replication_role = origin")
    stg.commit()
    print(f"[copy] DONE — {TID} -> staging ({total} rows across {len(tenant_tables)} tables)")


if __name__ == "__main__":
    main()
