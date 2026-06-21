# The GMP "data sponge" — absorb full energy history at onboarding

Ford's "data sponge" vision (Jun 2026): at onboarding, replay the captured GMP
session server-side and absorb the owner's ENTIRE energy history as their
system-of-record. Switching-cost = the moat. He chose the BIG version ("B" —
full energy record) over the cheap one ("A" — generation only), and wanted a
visible progress bar for the import.

(The live-telemetry POLLER half of the data hub — keeping current readings fresh
— is a separate system; see `data-hub-live-telemetry.md`. This file is the
HISTORY-absorb half.)

## Why GMP works where SMA can't (the replayability fork)
GMP's `/api/v2/accounts/{acct}/bills` returns the FULL history in ONE call and
the JWT is REPLAYABLE server-side (stored in `UtilitySession.api_token`). So the
backend can suck in years of bills with no browser in the loop. PROVEN on real
data: one tenant absorbed **2,924 bills spanning 16.4 YEARS** (back to 2010).
SMA/SmartHub CANNOT do this — httpOnly cookie / OAuth, no replayable bearer for
history (the VEC trap; see utility-meter-data-requirement.md). So "absorb their
whole history" is a GMP-class capability, not universal.

## Architecture (commit 750953f)
- `Bill` model (models.py) extended to the full energy record: `kwh_sent_to_grid,
  kwh_gross_generated, is_net_metered, total_cost, net_credit, avg_rate_cents_kwh,
  supplier`, and crucially `raw_json` (JSONB) = the ENTIRE bill. migrate.py adds
  these via explicit ALTER (existing prod `bills` table won't gain columns from
  create_all; brand-new tables do).
- `gmp.bill_json_to_metrics` → `_extract_full_record(bill)`: walks
  `billSegments[].segmentLineItems[]` by unitCode + reads top-level money keys.
  ALWAYS attaches `raw_json=bill`.
- `worker._upsert_bill`: persists the full record via a `_sponge` dict that NEVER
  overwrites a known value with None. `worker._pull_via_json` DROPPED the
  skip-no-kwh filter so ALL bills are absorbed (consumption-only too). SAFE for
  NEPOOL: every report writer guards on kwh<=0 — `bill_attribution.
  distribute_kwh_by_calendar_day` returns `{}` for no-generation bills (verified
  gmcs_writer/default_writer/account.py).
- `sponge.absorb_history(tenant_id, provider="gmp")`: orchestrates per-account
  using the proven `worker._pull_via_json`, updating a `SpongeProgress` row after
  each account (status / accounts_done / bills_absorbed / years_covered / message)
  so the UI shows a real progress bar. Fired in a daemon Thread on GMP capture in
  app.py's `/v1/sync` (replaces the plain post-sync pull for provider=="gmp").
- `SpongeProgress` model: one row per (tenant, provider); `sponge_status()` returns
  a pct for the bar. Account API: `GET /v1/account/sponge` (poll) + `GET
  /v1/account/energy-history` (the absorbed record + summary headline).

tests/test_sponge.py: full-record extraction, raw_json retention, consumption-only
absorb, double-count regression.

## THE TRANSFERABLE LESSON — raw_json backstop = "absorb now, map field-names later"
The single most reusable pattern from this build. When you absorb a vendor payload
whose exact field names you HAVEN'T verified against a real HAR, store the ENTIRE
raw payload alongside your modeled columns. Modeled fields are a convenience layer;
raw_json is authoritative.

This session it paid off immediately: generation mapped correctly (unitCode/GENERATE
was HAR-verified) but my GUESSED unitCodes for consumption + top-level keys for cost
MISSED → the live run showed `bills_with_cost: 0`, `bills_with_consumption: 0`
across all 2,924 bills. Because raw_json holds 100% of the real data, the fix is a
PURE parser correction + re-derive from stored raw_json — NO re-pull. Without
raw_json that data would have been permanently lost. To find the real field names,
introspect a stored `raw_json` on prod (`SELECT raw_json` → list billSegments
line-item unitCodes + top-level keys) rather than guessing again — the structure
is already saved.

Also a double-count bug the test caught: `_SENT_CODES` initially included
`GENERATE`, so sent-to-grid summed the generation line (900+500=1400). Gross
generation ≠ sent-to-grid — keep them disjoint.

## Live-proof discipline (Ford's rule)
Both halves of this build passed unit tests but the LIVE run on prod is what
surfaced the cost/consumption field-name miss. Green tests with mocked payloads
prove code shape, not that real vendor data matches your assumptions. Always run
`absorb_history` on a REAL GMP tenant via `railway ssh` and check
bills_with_cost/consumption/generation counts before claiming the sponge is
complete. (railway-ssh probe pattern: `B64=$(base64 -w0 /tmp/x.py); railway ssh
"cd /app && echo $B64 | base64 -d | python"`; wait for the new container by
looping an import of a new symbol until it prints NEW.)

## models.py editing hazard (bit me this session)
A patch that replaced the `Job` class header accidentally deleted its whole body
(the old_string matched only the docstring + `__tablename__`). When patching a
model class, include enough of the body in old_string to anchor uniquely, and
re-import after (`python -c "from api.models import X"`) to confirm the class
survived intact before moving on.
