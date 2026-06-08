# Archived scripts

One-shot historical scripts kept for reference only. Do NOT run without re-reading carefully — they were written for a specific moment in time and may not be safe against the current schema.

- `email_bruce_extension*.py` — three iterations of a manual extension-email blast to Bruce. Superseded once the Chrome Web Store listing went live.
- `scrape_2026_csv.py` / `scrape_2026_bills_json.py` — one-time GMP backfill from June 1 2026. Bills are now captured live by the extension.
- `setup_bruce_arrays.py` — one-time seed of Bruce's 7 arrays. Bruce's tenant was wiped Jun 8 2026 for clean re-signup; this script's hardcoded array names/IDs no longer correspond to any live tenant.
- `wipe_all_tenants.py` — DESTRUCTIVE nuclear option (TRUNCATE tenants CASCADE). Archived to keep it out of the main `scripts/` autocomplete surface. Restore only with intent.

Active script for deleting a single tenant: `scripts/delete_tenant_by_email.py`.
