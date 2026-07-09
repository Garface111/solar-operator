#!/usr/bin/env python3
"""Apply workflow-confirmed SmartHub hosts to the provider CSVs, then the extension
registry is regenerated separately (scripts/gen_smarthub_registry_js.py).

Input: a JSON file, [{"code": "...", "host": "xxx.smarthub.coop"}, ...] -- the
independently-verified hosts from the utility-smarthub-discovery workflow. For each
code, find its row across api/data/providers/*.csv and flip it live:
  scrape_status -> "live"; smarthub_host -> host; portal_url -> https://host;
  notes -> prepend a dated confirmation marker (keeps the old note for history).

Idempotent + safe: only touches rows whose code matches AND whose host isn't already
set to the same value; never creates rows; reports every code it couldn't find.

Usage: python scripts/apply_confirmed_smarthub_hosts.py <confirmed.json> [--dry-run]
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_DIR = os.path.join(HERE, "..", "api", "data", "providers")
STAMP = "Confirmed live SmartHub host via discovery sweep 2026-07-09."


def _norm_host(h: str) -> str:
    h = (h or "").strip().lower()
    h = h.replace("https://", "").replace("http://", "").strip("/")
    return h


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: apply_confirmed_smarthub_hosts.py <confirmed.json> [--dry-run]", file=sys.stderr)
        return 2
    dry = "--dry-run" in sys.argv
    confirmed = json.load(open(sys.argv[1]))
    by_code = {}
    for c in confirmed:
        code = (c.get("code") or "").strip().lower()
        host = _norm_host(c.get("host"))
        if code and host.endswith(".smarthub.coop"):
            by_code[code] = host
    print(f"{len(by_code)} confirmed codes to apply")

    applied, skipped_same, not_found = 0, 0, set(by_code)
    for path in sorted(glob.glob(os.path.join(PROVIDERS_DIR, "*.csv"))):
        rows, changed = [], False
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames
            for row in reader:
                code = (row.get("code") or "").strip().lower()
                if code in by_code:
                    not_found.discard(code)
                    host = by_code[code]
                    if _norm_host(row.get("smarthub_host")) == host and row.get("scrape_status") == "live":
                        skipped_same += 1
                    else:
                        row["scrape_status"] = "live"
                        row["smarthub_host"] = host
                        row["portal_url"] = f"https://{host}"
                        old = (row.get("notes") or "").strip()
                        row["notes"] = f"{STAMP} {old}".strip()
                        changed = True
                        applied += 1
                rows.append(row)
        if changed and not dry:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)

    print(f"applied={applied} skipped_already_live={skipped_same} not_found={len(not_found)}{' (DRY RUN)' if dry else ''}")
    if not_found:
        print("NOT FOUND (codes with no CSV row):", ", ".join(sorted(not_found))[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
