"""READ-ONLY: verify report_has_data() matches the coverage diagnostic on prod.

For every active NEPOOL client, prints whether report_has_data() returns True/
False, and the count. This is the exact function the scheduler now uses to skip
empty reports — confirm it flags the known-empty clients and passes the good ones.
NO WRITES.
"""
from __future__ import annotations
from sqlalchemy import select
from api.db import SessionLocal
from api.models import Tenant, Client
from api.writers.gmcs_writer import report_has_data


def main():
    with SessionLocal() as db:
        tenants = db.execute(select(Tenant).where(Tenant.product == "nepool")).scalars().all()
        tenants = [t for t in tenants
                   if (t.active or t.subscription_status in ("comped", "trialing"))]
        client_rows = []
        for t in tenants:
            cs = db.execute(select(Client).where(
                Client.tenant_id == t.id, Client.deleted_at.is_(None),
                Client.active.is_(True))).scalars().all()
            for c in cs:
                client_rows.append((t, c))

    has, empty = 0, 0
    print("WOULD-SEND vs WOULD-SKIP (report_has_data):")
    for (t, c) in client_rows:
        ok = report_has_data(c.id)
        has += ok
        empty += (not ok)
        if not ok:
            print(f"  SKIP  client {c.id:<5} {c.name[:30]:<30} tenant={t.id} "
                  f"status={t.subscription_status} contact={c.contact_email!r}")
    print(f"\nTotal active NEPOOL clients: {len(client_rows)}")
    print(f"  WOULD SEND (has data): {has}")
    print(f"  WOULD SKIP (empty):    {empty}")


if __name__ == "__main__":
    main()
