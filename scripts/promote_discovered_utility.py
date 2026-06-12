#!/usr/bin/env python3
"""Promote a discovered SmartHub utility to the curated catalog.

When a capture lands from a *.smarthub.coop host that isn't in
api/data/providers/*.csv, /v1/sync mints a discovered provider code
("sh_<subdomain>") and records a DiscoveredUtility row. Data flows
immediately; this script graduates the utility to first-class:

    python -m scripts.promote_discovered_utility <host> \
        --code <curated_code> --label "Real Utility Name" --state VT

It will:
  1. Append the row to api/data/providers/<STATE>.csv
  2. Regenerate extension/smarthub_registry.js
  3. Backfill UtilityAccount / UtilitySession provider columns from the
     discovered sh_* code to the curated code
  4. Mark the DiscoveredUtility row promoted

Run against prod via:  railway ssh "cd /app && python -m scripts.promote_discovered_utility ..."
(steps 1-2 are repo-side; on prod only the DB backfill (3-4) executes —
the CSV/registry changes ship with the next deploy.)
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update  # noqa: E402

from api.db import SessionLocal  # noqa: E402
from api.models import DiscoveredUtility, UtilityAccount, UtilitySession  # noqa: E402
from api.adapters.smarthub import derive_provider_from_host  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", help="e.g. norwichsolar.smarthub.coop")
    ap.add_argument("--code", help="curated provider code (default: subdomain)")
    ap.add_argument("--label", help="utility display name for the catalog")
    ap.add_argument("--state", default="XX", help="2-letter state for the CSV file")
    ap.add_argument("--db-only", action="store_true",
                    help="skip CSV/registry edits (use on prod where the repo is read-only)")
    args = ap.parse_args()

    host = args.host.strip().lower()
    derived = derive_provider_from_host(host)
    if not derived:
        print(f"ERROR: {host} is not a smarthub.coop host")
        return 1
    old_code = derived["provider"] if derived["discovered"] else None

    sub = host.replace(".smarthub.coop", "")
    new_code = (args.code or sub.replace("-", "_")).strip().lower()
    label = args.label or derived["name"]

    # ── 1+2: repo-side catalog edit + registry regen ─────────────────────
    if not args.db_only:
        csv_path = ROOT / "api" / "data" / "providers" / f"{args.state.upper()}.csv"
        if not csv_path.exists():
            print(f"ERROR: {csv_path} does not exist — pass the right --state")
            return 1
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
            fieldnames = rows[0].keys() if rows else (
                "code,label,state,scrape_status,smarthub_host,portal_url,notes".split(","))
        if any((r.get("smarthub_host") or "").strip() == host for r in rows):
            print(f"already in catalog: {host}")
        else:
            with open(csv_path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writerow({
                    "code": new_code, "label": label, "state": args.state.upper(),
                    "scrape_status": "live", "smarthub_host": host,
                    "portal_url": f"https://{host}",
                    "notes": "Promoted from fleet discovery — first capture verified in the wild.",
                })
            print(f"✓ appended {new_code} to {csv_path.name}")
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "gen_smarthub_registry_js.py")],
                check=True,
            )
            print("✓ regenerated extension/smarthub_registry.js")

    # ── 3+4: DB backfill ─────────────────────────────────────────────────
    if old_code is None:
        print("host already curated — no sh_* rows to backfill")
        return 0
    with SessionLocal() as db:
        n_acct = db.execute(
            update(UtilityAccount).where(UtilityAccount.provider == old_code)
            .values(provider=new_code)
        ).rowcount
        n_sess = db.execute(
            update(UtilitySession).where(UtilitySession.provider == old_code)
            .values(provider=new_code)
        ).rowcount
        disc = db.execute(
            select(DiscoveredUtility).where(DiscoveredUtility.host == host)
        ).scalar_one_or_none()
        if disc:
            disc.promoted_code = new_code
        db.commit()
        print(f"✓ backfilled {n_acct} account(s), {n_sess} session(s): {old_code} → {new_code}")
        if disc:
            print(f"✓ marked {host} promoted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
