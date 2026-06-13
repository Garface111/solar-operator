# Provider catalog — how to add utilities (swarm-safe)

NEPOOL Operator's supported-utility catalog is **data, not code**. The single
source of truth is the per-state CSV files in this directory:

    api/data/providers/<STATE>.csv     e.g. VT.csv, NH.csv, MA.csv, ME.csv
    api/data/providers/_core.csv       multi-state / generic fallbacks only

`api/providers.py` loads, validates, and merges these at import. The SmartHub
universal adapter and the Chrome extension registry are both DERIVED from them.
You never edit Python or JS to add a utility — you edit one CSV.

## Why one file per state (the swarm rule)

We are building this out nationwide with parallel agents. **One lane owns one
state file.** Because two lanes never touch the same file, there are **zero
merge conflicts by construction.** Do not put a utility in a state file that
isn't its state. Do not edit `_core.csv` in a state lane.

## CSV schema (header row required)

    code,label,state,scrape_status,smarthub_host,portal_url,notes

| column         | rule |
|----------------|------|
| `code`         | lowercase, unique across ALL files, `[a-z0-9_]+`. Stored in the DB. Never reuse/rename an existing code. |
| `label`        | human name shown in the UI dropdown |
| `state`        | two-letter (VT, NH, MA, ME, RI, CT). Must match the filename. |
| `scrape_status`| `live` \| `in-progress` \| `manual` (see below) |
| `smarthub_host`| `<utility>.smarthub.coop` — set IFF this is a wired SmartHub utility; else empty |
| `portal_url`   | customer login URL (optional) |
| `notes`        | caveats, flagged LOUDLY. Quote the cell if it contains commas. |

### scrape_status — and the ONE rule you cannot break

- **`live`** — automated scraping works *today*. A `live` row MUST be either:
  - a **SmartHub** utility with a `smarthub_host` that actually resolves, OR
  - one of the known bespoke adapters (`gmp`).
  The loader REJECTS a `live` row that is neither. This is enforced because a
  fabricated scraper silently produces **wrong kWh on a customer's NEPOOL
  report** — the single failure mode this product cannot tolerate.
- **`in-progress`** — a real portal exists but we haven't built/verified the
  adapter. Surfaces in onboarding with the manual-PDF-upload route. This is the
  honest default for any investor-owned or bespoke portal you can't verify.
- **`manual`** — no public API/portal at all; customer emails PDFs we OCR.

**If you cannot confirm it, it is NOT `live`.** Never guess a SmartHub host —
verify it resolves first (below). When unsure, use `in-progress`.

## How to classify a utility (do the recon, don't guess)

1. **Is it on NISC SmartHub?** Co-ops and municipals usually are; investor-owned
   never are. Probe candidate hosts:
   ```bash
   for h in <utility>.smarthub.coop <city>electric.smarthub.coop villageof<x>.smarthub.coop; do
     printf '%s -> ' "$h"; curl -sI -m 8 -o /dev/null -w '%{http_code}\n' "https://$h"
   done
   ```
   A `200`/`301`/`302` = it resolves → SmartHub. `000` = no such host.
   If a host resolves, add the row with that exact `smarthub_host` and
   `scrape_status=live`.
2. **Investor-owned / bespoke portal** (Eversource, National Grid, Unitil,
   Liberty/AQN, United Illuminating, etc.): never SmartHub. Add with
   `scrape_status=in-progress`, `smarthub_host` empty, `portal_url` = the real
   customer login if you found one. Note in `notes` that a live login is needed
   to build the adapter.
3. **No portal found at all:** `scrape_status=manual`.

## After editing a CSV — regenerate + validate (REQUIRED)

```bash
cd ~/solar-operator
source venv/bin/activate
python scripts/gen_smarthub_registry_js.py        # regenerate extension JS from CSVs
python -m pytest tests/test_provider_registry.py tests/test_smarthub_adapter.py -q
python -c "from api.providers import PROVIDERS; print(len(PROVIDERS), 'providers OK')"
```

If `test_provider_registry.py` fails, the catalog is malformed — fix the CSV;
do not edit generated files by hand. The extension JS
(`extension/smarthub_registry.js`) is GENERATED; never hand-edit it.

## What ships, when

`GET /v1/providers` (public, no auth) reads `PROVIDERS` live, so a CSV change
goes live the moment the container deploys — **no frontend rebuild needed** for
the dropdown. The Chrome extension registry change ships with the next
extension build (it's a generated static file in `extension/`).
