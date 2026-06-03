# Solar Operator

SaaS that automates quarterly net-metering credit reports for VT community solar operators. Replaces a ~$135/array/quarter human consultant with $250 setup + $45/array/month software.

## Stack
- **Backend:** Python (FastAPI-style app in `api/`), Postgres on Railway
- **Frontend:** Chrome MV3 extension (`extension/`) — scrapes GMP utility portal
- **Email:** Resend SDK only. FROM: `admin@solaroperator.org`
- **Payments:** Stripe LIVE
- **Hosting:** Netlify (marketing) + Railway (API/DB/workers)
- **Domain:** solaroperator.org

## Key directories
- `api/app.py` — FastAPI app entrypoint
- `api/models.py` — SQLAlchemy models (Tenant, Array, UtilityAccount, etc.)
- `api/writers/gmcs_writer.py` — **DEFAULT** Excel report writer. Pixel-matches Bruce Genereaux's GMCS.xlsx master format.
- `api/writers/default_writer.py` — legacy writer, kept for fallback only
- `api/adapters/` — utility portal scraping adapters (GMP, etc.)
- `api/scheduler.py` — quarterly run scheduler
- `api/worker.py` — background job worker
- `api/migrate.py` — DB migrations (run via `railway ssh "cd /app && python -m api.migrate"`)
- `extension/` — Chrome MV3 capture extension (currently v1.0.1 pending Chrome Store push)
- `scripts/` — one-off ops scripts

## GMCS writer format rules (CRITICAL — do not break)
- One sheet per array
- A1:C1 merged, value = `"<Array Name> (<NEPOOL-GIS ID>)"`
- Row 5 = header row, font size 14
- Rolling 6 quarters, each quarter is 3 month rows + 1 gap row
- MWh format = Excel "General" (e.g., 25.720 displays as 25.72)
- RECs = `int(mwh)` per month (floor — only safe because MWh is always ≥ 0)
- Footnote text is VERBATIM — never paraphrase. Pinned to row 31 unless data extends past it (`foot_row = 31 if row <= 31 else row`)
- Column widths: A,B,C,D all 24.0 (Ford prefers wider than original)
- `Array.nepool_gis_id` is the canonical field (migrated from older naming)

## Tenant + Client model
Tenant → Client → Array → UtilityAccount. **Client layer is IMPLEMENTED** (post Phase 1). `Array.client_id` is the FK; `build_workbook(client_id=...)` is the preferred entrypoint, `tenant_id` accepted for legacy callers.

Bruce Genereaux (tenant `ten_14b76982523a3b47`) is the live pilot — 7 arrays, 9 GMP accounts, comped. His Starlake array sums 3 sub-meters and uses `bill_offset_months=0` (same-month, unlike others which are prior-month).

## Code standards
- Python 3.11+
- Type hints on public functions
- 4-space indent Python, 2-space YAML/JSON
- No wildcard imports
- Tests with non-Bruce data when possible (avoid overfitting to one customer)

## Workflow rules
- Flag uncertainty/caveats LOUDLY, not buried. Ford trust-checks output.
- Prefer proper fixes over workarounds; surface root cause.
- Pricing decisions come LAST, after product/data integrity is proven.
- For data/infra work, lay out speed/cost/accuracy tradeoffs BEFORE executing.

## Common commands
- `cd ~/solar-operator && source venv/bin/activate` — activate venv
- `python -m api.app` — run API locally (check `dev_server.py` for dev entry)
- `railway ssh "cd /app && python -m api.migrate"` — run migrations on prod
- `railway logs` — view prod logs
- `git push` — Railway auto-deploys from main
