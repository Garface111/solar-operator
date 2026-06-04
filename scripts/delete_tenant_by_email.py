"""Delete tenants matching an email pattern (case-insensitive substring).

Usage: railway ssh "cd /app && python -m scripts.delete_tenant_by_email <pattern>"
Example: python -m scripts.delete_tenant_by_email ford.genereaux
"""
import sys
from sqlalchemy import text
from api.db import SessionLocal

if len(sys.argv) < 2:
    print("Usage: python -m scripts.delete_tenant_by_email <email_substring>")
    sys.exit(1)

pat = sys.argv[1].lower()

with SessionLocal() as db:
    bind = db.get_bind()
    rows = db.execute(
        text("SELECT id, email, name, active, created_at FROM tenants WHERE lower(email) LIKE :p ORDER BY created_at DESC"),
        {"p": f"%{pat}%"},
    ).all()
    if not rows:
        print(f"No tenants matched '{pat}'.")
        sys.exit(0)
    print(f"Matched {len(rows)} tenant(s):")
    for r in rows:
        print(f"  {r.id} | {r.email} | {r.name} | active={r.active} | {r.created_at}")
    ids = [r.id for r in rows]
    # TRUNCATE/DELETE CASCADE-equivalent: delete tenant rows, FK CASCADE handles the rest
    with bind.begin() as conn:
        conn.execute(
            text("DELETE FROM tenants WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
    print(f"\nDeleted {len(ids)} tenant(s) and all dependent rows (CASCADE).")
