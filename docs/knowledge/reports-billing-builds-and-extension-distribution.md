# Reports/billing builds + extension distribution (AO)

Durable build patterns from the Reports/billing + Chrome-extension work (Jun 2026).

## Multi-array offtaker billing (one combined invoice across N arrays)
An offtaker can own a share of SEVERAL arrays → ONE invoice summing per-array
shares. Pattern, additive + back-compat:
- Model: `BillingReportSubscription.array_allocations` JSON =
  `[{array_id, allocation_pct(0..1)}]`. NULL/empty → legacy single
  `array_id`/`allocation_pct` path runs UNCHANGED. Migration: `ADD COLUMN
  array_allocations JSON` (idempotent via column_exists; JSON works sqlite+PG).
- Math: `delivery.build_manual_match` — when allocations present, loop each
  array's `_array_period_kwh_sourced` (GMP daily-read → DailyGen/Bill fallback),
  sum `array_kwh × pct`, build `array_breakdown` list. Keep first alloc in the
  legacy array_id/allocation_pct cols for list views.
- Invoice PDF (invoice.py): `invoice_for_period` must FORWARD `array_breakdown`
  from `match.project_totals`/`computed_invoice` into `inv` (the renderer rebuilds
  `inv` via compute_invoice and would otherwise drop it — same strip-on-rebuild
  class as fleet-store). Render a "Your share by array" table (one line per array
  → summed Total) above the line items, only when len(breakdown) > 1.
- Endpoint: `/subscriptions` accepts `array_allocations` as a JSON STRING form
  field; validate each {array_id, pct in (0,1]} + tenant-owns-array.
- UI step 3 (reports.js): checkbox-per-array + a per-array `%` input enabled when
  checked; finish sends `array_allocations` JSON when >1 array. Single-array
  selection still supported. QA hooks: `window.__rbWizGoto(n)` /
  `__rbRenderWizard(state)` to jump wizard steps in Playwright without real auth.
- AO Array model ALREADY aggregates multiple GMP accounts ("Starlake = 3 GMP
  accounts summed"), so a picked Array bundles its GMP accounts. If Ford wants
  raw GMP account NUMBERS as separate pickable items, that's a different source.

## Wizard copy reframes (Ford's mental model — labels only)
Step 2 "Your rate": the rate USED is the one on the owner's current bill (auto
solar-credit rate, no entry needed); the value the owner ENTERS is the DISCOUNT
rate from their contract with the offtaker. Reframe copy + field labels to say
this; never touch internal IDs (rbWizNet/rbWizDisc/default_net_rate_per_kwh).
Dead UI: a green "set year →" span LOOKED tappable but had no handler (the real
control was the year input beside it) → render empty when unknown, not a fake CTA.

## Chrome extension distribution + self-diagnosing logs
- Distribute a build to a non-dev (e.g. Ford's dad Bruce) via a GitHub RELEASE
  with the zip as an asset (established pattern: tag `ext-vX.Y.Z`):
  `gh release create ext-vX.Y.Z "<zip>#name.zip" --target $(git rev-parse HEAD)
  --title ... --notes ...` (tag must be pushed OR use --target). VERIFY the asset
  downloads (curl -sL → HTTP 200, byte-match) before sending the link. Email via
  notify._send_via_resend(product="array_operator") + render_email_skin; include
  unzip → chrome://extensions → Developer mode → Load unpacked steps.
- Build the zip with `scripts/build_extension_zip.sh` (reads manifest version,
  drops to Desktop + Archives). BUMP manifest version every change; VERIFY the fix
  is actually IN the zip (unzip + grep) — never trust the build blindly.
- SELF-DIAGNOSING capture logs: when a content-script capture (Fronius
  solarweb_content.js) "does nothing" and the console is silent, the gates return
  silently. Add a load-time `LOG("content script loaded vX on <host>")` (absence =
  not injected) + per-tick gate logs (`intent: yes/NO`, `signed in: yes/NO`,
  `captured systems: N`). Turns a blind retry loop into "stopped at gate X".
- Fronius devwork series is WATTS not kW (a Primo 12.5kW read ~1699 = 1.7kW, not
  1699kW) → ÷1000 once. Solar.web chart cadence ~30min → live-fresh window must be
  ≥60min or genuinely-recent readings get rejected as stale.

## PDF brand kit
Invoice + performance-summary + quarterly share `api/billing/_pdf_brand.py` (one
palette + dark "energy" hero band + green-gradient energy bar). Quarterly is just
a cadence — no separate generator. Rasterize with pymupdf → vision_analyze every
PDF change; honest "—" for metrics with no data, never fabricate.
