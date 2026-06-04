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
        text("SELECT id, contact_email, name, active, created_at FROM tenants WHERE lower(contact_email) LIKE :p ORDER BY created_at DESC"),
        {"p": f"%{pat}%"},
    ).all()
    if not rows:
        print(f"No tenants matched '{pat}'.")
        sys.exit(0)
    print(f"Matched {len(rows)} tenant(s):")
    for r in rows:
        print(f"  {r.id} | {r.contact_email} | {r.name} | active={r.active} | {r.created_at}")
    ids = [r.id for r in rows]
    # Cascade-delete via per-table DELETE in dependency order (FKs are not
    # declared ON DELETE CASCADE at the schema level).
    with bind.begin() as conn:
        # Find arrays + utility_accounts owned by these tenants for nested deletes
        for tid in ids:
            conn.execute(text("DELETE FROM bills WHERE account_id IN (SELECT id FROM utility_accounts WHERE tenant_id = :t)"), {"t": tid})
            conn.execute(text("DELETE FROM utility_accounts WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM utility_sessions WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM login_tokens WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM arrays WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tid})
            # tenant_templates if present (best effort — table may not exist on older deploys)
            try:
                conn.execute(text("DELETE FROM tenant_templates WHERE tenant_id = :t"), {"t": tid})
            except Exception:
                pass
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
    print(f"\nDeleted {len(ids)} tenant(s) and all dependent rows.")
