"""Hard-delete ONE tenant and every row that hangs off it, for good.

Usage (prod):
  railway ssh --service web "cd /app && python -m scripts.hard_delete_tenant <tenant_id|email> [--yes-i-mean-it] [--allow-stripe]"

Without --yes-i-mean-it it is a DRY RUN: prints the full cascade plan with
row counts and exits. With it, it:
  1. refuses if the tenant still has a Stripe customer/subscription id
     (money is Ford's call: cancel in Stripe first, or pass --allow-stripe
     when billing has already been dealt with),
  2. zeroes the Cloud Capture vault + utility session tokens
     (api.vault_lifecycle.purge_tenant_sensitive_data),
  3. prints a base64 JSON snapshot of the small, hand-built tables
     (tenant, clients, arrays, utility_accounts, inverters, ...) so the
     shape of the account can be reconstructed if this was a mistake.
     Bulk generation/bill rows are NOT snapshotted (re-harvestable),
  4. walks the live FK graph from information_schema and deletes
     children-first inside one transaction, then deletes rows in every
     table that carries a tenant_id column WITHOUT a declared FK
     (jobs, harvest_run, ea_*, feature_suggestions, ...),
  5. re-counts everything and prints whether anything is left.

It replaces scripts/delete_tenant_by_email.py, which hard-coded 7 tables
and silently left ~40 others orphaned.
"""
from __future__ import annotations

import base64
import json
import sys

from sqlalchemy import text

from api.db import SessionLocal, engine

SNAPSHOT_MAX_ROWS = 5000  # tables bigger than this are bulk telemetry: skip


def resolve_tenant(db, ident: str):
    ident = ident.strip()
    if "@" in ident:
        rows = db.execute(
            text("SELECT id FROM tenants WHERE lower(contact_email)=lower(:e)"), {"e": ident}
        ).all()
    else:
        rows = db.execute(text("SELECT id FROM tenants WHERE id=:i"), {"i": ident}).all()
    if len(rows) != 1:
        print(f"Expected exactly 1 tenant for {ident!r}, found {len(rows)}. Refusing.")
        sys.exit(2)
    return rows[0][0]


def fk_graph(db):
    fks = db.execute(text("""
        SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name=tc.constraint_name
        WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'
    """)).all()
    children: dict[str, list[tuple[str, str, str]]] = {}
    for t, c, rt, rc in fks:
        children.setdefault(rt, []).append((t, c, rc))
    fk_tenant_cols = {(t, c) for t, c, rt, _ in fks if rt == "tenants"}
    return children, fk_tenant_cols


def build_plan(db, tid: str):
    """Ordered list of (depth, table, where_sql): children before parents."""
    children, fk_tenant_cols = fk_graph(db)
    plan: list[tuple[int, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def visit(table: str, where: str, depth: int):
        if (table, where) in seen or depth > 12:
            return
        seen.add((table, where))
        for ct, cc, rc in children.get(table, []):
            sub = f"{cc} IN (SELECT {rc} FROM {table} WHERE {where})"
            if ct == table:
                # self-reference: one level, no recursion
                if (ct, sub) not in seen:
                    seen.add((ct, sub))
                    plan.append((depth + 1, ct, sub))
                continue
            visit(ct, sub, depth + 1)
        plan.append((depth, table, where))

    # tables that carry tenant_id but no declared FK are roots of their own
    no_fk = [
        t for (t,) in db.execute(text(
            "SELECT DISTINCT table_name FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name='tenant_id'"
        )).all()
        if (t, "tenant_id") not in fk_tenant_cols and t != "tenants"
    ]
    for t in sorted(no_fk):
        visit(t, "tenant_id=:t", 1)
    visit("tenants", "id=:t", 0)
    return plan, no_fk


def count(db, table, where, tid):
    try:
        return db.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), {"t": tid}).scalar()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return f"ERR {type(e).__name__}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        sys.exit(1)
    live = "--yes-i-mean-it" in flags

    with SessionLocal() as db:
        tid = resolve_tenant(db, args[0])
        t = db.execute(text(
            "SELECT id,name,contact_email,active,subscription_status,"
            "stripe_customer_id,stripe_subscription_id FROM tenants WHERE id=:t"
        ), {"t": tid}).first()
        print(f"Tenant: {t.id} | {t.name} | {t.contact_email} | active={t.active} | {t.subscription_status}")
        if (t.stripe_customer_id or t.stripe_subscription_id) and "--allow-stripe" not in flags:
            print(f"REFUSING: tenant has Stripe ids (cus={t.stripe_customer_id} sub={t.stripe_subscription_id}). "
                  "Deal with billing first, then pass --allow-stripe.")
            sys.exit(3)

        plan, no_fk = build_plan(db, tid)
        db.execute(text("SET statement_timeout = 0"))
        nonzero = []
        for depth, table, where in plan:
            n = count(db, table, where, tid)
            if n:
                nonzero.append((depth, table, where, n))
        print(f"\nCascade plan: {len(plan)} steps, {len(nonzero)} with rows. No-FK tenant_id tables: {', '.join(no_fk)}")
        for depth, table, where, n in nonzero:
            print(f"  {'  ' * depth}{table}: {n}")
        if not live:
            print("\nDRY RUN, nothing deleted. Re-run with --yes-i-mean-it.")
            return

        # ---- 1. vault + session tokens (ORM path, never logs secrets)
        try:
            from api.vault_lifecycle import purge_tenant_sensitive_data
            purge = purge_tenant_sensitive_data(db, tid, reason="hard_delete_tenant")
            db.commit()
            print(f"\nVault purge: {purge.get('counts')}")
        except Exception as e:  # noqa: BLE001
            db.rollback()
            print(f"\nVault purge skipped ({type(e).__name__}: {e}); raw cascade will still remove the rows.")

        # ---- 2. snapshot of small tables
        snap: dict[str, list] = {}
        for depth, table, where, n in nonzero:
            if isinstance(n, int) and 0 < n <= SNAPSHOT_MAX_ROWS and table not in snap:
                rows = db.execute(text(f"SELECT * FROM {table} WHERE {where}"), {"t": tid}).mappings().all()
                snap[table] = [dict(r) for r in rows]
        blob = base64.b64encode(json.dumps(snap, default=str).encode()).decode()
        print(f"\nSNAPSHOT tables={list(snap)} bytes={len(blob)}")
        print(f"SNAPSHOT_B64:{blob}")

    # ---- 3. the cascade, one transaction
    deleted: dict[str, int] = {}
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = 0"))
        for depth, table, where in plan:
            try:
                r = conn.execute(text(f"DELETE FROM {table} WHERE {where}"), {"t": tid})
                if r.rowcount:
                    deleted[table] = deleted.get(table, 0) + r.rowcount
            except Exception as e:  # noqa: BLE001
                print(f"FAILED at {table} [{where[:80]}]: {e}")
                raise
    print("\nDELETED:")
    for k, v in sorted(deleted.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    print(f"  TOTAL: {sum(deleted.values())}")

    # ---- 4. verify
    with engine.connect() as conn:
        left = conn.execute(text("SELECT count(*) FROM tenants WHERE id=:t"), {"t": tid}).scalar()
        residue = []
        for tname in no_fk:
            n = conn.execute(text(f"SELECT count(*) FROM {tname} WHERE tenant_id=:t"), {"t": tid}).scalar()
            if n:
                residue.append((tname, n))
    print(f"\nVERIFY: tenants rows left={left}; no-FK residue={residue or 'none'}")
    print("DONE" if (left == 0 and not residue) else "INCOMPLETE, inspect above")


if __name__ == "__main__":
    main()
