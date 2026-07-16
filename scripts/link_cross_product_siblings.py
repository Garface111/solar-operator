"""Backfill: link EVERY email that owns both a NEPOOL and an Array Operator
tenant as cross-product siblings (bidirectional linked_tenant_id). Idempotent
and reversible — reuses tenant_link.link_by_email (canonical selection, no stale
duplicates). Ford 2026-07-16: same-email NO+AO accounts should work together.

  python -m scripts.link_cross_product_siblings            # DRY RUN (report only)
  python -m scripts.link_cross_product_siblings --apply     # write the links
"""
from __future__ import annotations
import argparse
from sqlalchemy import func, select
from api.db import SessionLocal
from api.models import Tenant
from api.tenant_link import link_by_email, normalize_email


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit links (default: dry run)")
    args = ap.parse_args()

    with SessionLocal() as db:
        rows = db.execute(
            select(Tenant.contact_email, Tenant.product)
        ).all()

    by_email: dict[str, set[str]] = {}
    for email, product in rows:
        e = normalize_email(email)
        if not e:
            continue
        by_email.setdefault(e, set()).add((product or "nepool"))

    both = [e for e, prods in by_email.items()
            if "nepool" in prods and "array_operator" in prods]
    print(f"emails owning BOTH products: {len(both)}")

    linked = skipped = 0
    for e in sorted(both):
        res = link_by_email(e, apply=args.apply)
        state = res.get("reason")
        nep = (res.get("nepool_tenant") or {}).get("id")
        ao = (res.get("array_operator_tenant") or {}).get("id")
        did = res.get("linked")
        if did and state in ("linked",):
            linked += 1
        elif state == "already-linked":
            skipped += 1
        print(f"  {e:<40} nepool={nep} ao={ao} -> {state}")

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: "
          f"newly-linked={linked} already-linked={skipped} total-both={len(both)}")


if __name__ == "__main__":
    main()
