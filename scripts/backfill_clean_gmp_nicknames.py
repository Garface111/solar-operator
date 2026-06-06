"""One-shot backfill: clean GMP positional prefixes from existing UtilityAccount nicknames.

Usage:
    python scripts/backfill_clean_gmp_nicknames.py            # dry-run (default)
    python scripts/backfill_clean_gmp_nicknames.py --apply    # commit changes

For each UtilityAccount where provider='gmp':
  - Computes clean_gmp_nickname(original_nickname)
  - If different, prints and (on --apply) updates the account
  - If account.array_id is set AND Array.name still equals the raw nickname
    (operator hasn't manually renamed), updates the array name too

Prints a summary at the end: "Changed N of M accounts".
"""
from __future__ import annotations

import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.db import SessionLocal
from api.models import Array, UtilityAccount
from api.adapters._gmp_clean import clean_gmp_nickname


def run(apply: bool) -> None:
    label = "APPLY" if apply else "DRY-RUN"
    print(f"[{label}] Scanning GMP utility accounts for dirty nicknames...")

    changed = 0
    total = 0

    with SessionLocal() as db:
        try:
            accounts = (
                db.query(UtilityAccount)
                .filter(
                    UtilityAccount.provider == "gmp",
                    UtilityAccount.deleted_at.is_(None),
                    UtilityAccount.nickname.isnot(None),
                )
                .all()
            )
        except Exception as exc:
            print(f"[ERROR] Could not query DB: {exc}")
            print("  (Is DATABASE_URL set and migrations run? For prod: set DATABASE_URL to the Railway URL.)")
            return

        total = len(accounts)

        for acct in accounts:
            orig = acct.nickname
            clean = clean_gmp_nickname(orig)
            if clean == orig:
                continue

            changed += 1
            print(f"  [{acct.tenant_id}] account {acct.id}: {orig!r} → {clean!r}")

            if apply:
                acct.nickname = clean

                # Also update Array.name if it still matches the raw nickname
                if acct.array_id is not None:
                    arr = db.get(Array, acct.array_id)
                    if arr is not None and arr.name == orig:
                        print(f"    └─ array {arr.id} name: {arr.name!r} → {clean!r}")
                        arr.name = clean

        if apply:
            db.commit()
            print(f"\n[APPLY] Committed. Changed {changed} of {total} accounts.")
        else:
            print(f"\n[DRY-RUN] Would change {changed} of {total} accounts. Re-run with --apply to commit.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    run(apply=apply)
