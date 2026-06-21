# Fleet audit / reconciliation — engine, data plumbing, and integrity gates

The production-vs-settlement AUDITOR is Ford's core moat ("reconcile inverter
PRODUCTION vs utility-SETTLED kWh, flag leaks, sell to investor-owners"). The
engine already exists; the recurring work is DATA PLUMBING and INTEGRITY, not
building the auditor. This ref consolidates what's built + the traps.

## Where it lives
- `api/reconciliation/reconcile.py::reconcile_array(db, array_id, ws, we, rate)`
  → `ReconResult` (settlement_kwh, production_kwh, coverage_ratio, variance_pct,
  dollars_at_risk, status, report_leak, gates{}, notes[]). Read-only on the DB.
- `api/reconciliation/classify.py::classify_array` → single_site vs
  group_net_metered. CALIBRATION (real-data): the `groupNetMetered` JSON flag is
  set on ~63/64 arrays = PARTICIPATION, not host-meter role; the authoritative
  group signal is the bill raw_text marker "Group Excess Shared"/"Group Rate"
  (PRIMARY); the JSON flag is a weak corroborator.
- 3 legs of the moat: PRODUCTION (inverter vendor) × SETTLEMENT (utility bill) ×
  IRRADIANCE (`adapters/weather.py`, the independent physical driver).

## THE keystone trap — engine reads a DIFFERENT table than the feed writes
The auditor's production leg reads `DailyGeneration`. The GMP daily backfill
writes `gmp_daily_generation`. They were two ships passing → 0 auditable arrays
despite 232 having settlement bills. FIX (commit d822f01):
`reconcile._production_over_window` now MERGES both per-day — `DailyGeneration`
wins on overlap, the `gmp_daily_read.get_daily_series()` seam fills the gaps (no
double-count). Whenever a new production feed lands, CHECK it actually flows into
the table the audit reads, or the data is invisible to the audit even though it's
"in the system." (Same class of bug as the GMP backfill being git-untracked.)

## Integrity gate — never assert a leak from same-source data (Ford's honesty rule)
GMP interval data is the UTILITY'S OWN meter. Comparing it to the GMP bill is
reconciling the utility against itself — useful ("are you credited for metered
kWh?") but NOT an independent leak. So:
- `PRODUCTION_SOURCES` = everything that counts as metered production (added the
  missing `extension_pull_corrected` this session — was silently dropped).
- `INDEPENDENT_SOURCES` = ("solaredge","csv","manual") — a party independent of
  the utility.
- A variance backed only by utility-sourced production → status
  `leak_unconfirmed` (shows the dollars, sets `report_leak=False`, tells the owner
  "connect inverter monitoring to confirm"). A REAL asserted `leak` requires
  `gates["independent_feed"]` true. Onboarding inverter monitoring is what
  upgrades unconfirmed → confirmed.

## Run the live fleet audit (the prove-the-thesis fixture Ford asks for)
Use `scripts/fleet_audit_probe.py` (base64 → railway ssh against the prod image).
It reports total arrays, how many have settlement, how many have production, how
many are FULLY AUDITABLE (ok/leak), the status distribution, and the leak list.
Report the REAL numbers even when unflattering (Jun'26 baseline: 405 arrays, 232
settlement, only 2 production, 0 auditable, 0 leaks — correct refusal to
fabricate). The honest result tells you the next lever (coverage vs UI).

## Coverage is the real product gap, not the engine
Only ~48/405 arrays have ANY inverter connected (chint4/fronius15/sma6/
solaredge23). The other ~357 can't produce a production leg until their vendor is
onboarded. So when Ford asks "what's missing to make AO the auditor": the answer
is FEEDS first, then surface it. The face is now BUILT (Jun'26): the Audit tab.

## The Audit tab — surfacing the engine (the "give the brain a face" pattern)
Route: `GET /v1/array-owners/fleet-audit` (array_owners.py) — `_tenant_from_bearer`
auth, loops the tenant's arrays, wraps each `reconcile_array` in try/except so one
array can't 500 the whole fleet, returns `{summary{auditable,ok,leak,
leak_unconfirmed,insufficient_data,have_settlement,have_production,dollars_flagged,
coverage_pct}, arrays[...]}` sorted leaks-first. Verify: route in app.routes +
401 (gated) not 404 on prod.
Frontend = the reusable 4-edit AO tab-wiring contract (audit.js + audit.css):
1. index.html: `<a class="tab" id="tabAudit" href="#audit">` + `<section
   class="panel" id="panelAudit">` + the `<link audit.css>`/`<script audit.js>`.
2. sandbox.js TABS map: `audit:{panel:"panelAudit",tab:"tabAudit"}`.
3. sandbox.js `tabFromHash()`: `if(h==="#audit") return "audit"`.
4. sandbox.js `applyView()` dispatch: `else if(active==="audit"){ window.__aoLoadAudit&&window.__aoLoadAudit() }`.
audit.js exposes `window.__aoLoadAudit=load`, reads `localStorage["so_session"]`,
fetches with `Authorization: "Bearer "+s`, 401/403 → "session expired" state.
Slick styling reuses the site card vocabulary (gradient panels, 18–22px radii,
ambient glow, tabular-nums; status palette leak=#ff6b6b unconfirmed=gold
ok=#3fd68a partial=#5ec2ff none=faint; theme-agnostic via vars).
TWO UI pitfalls hit this session:
- Coverage RING (conic-gradient): the `::before` inner disc must be `z-index:-1`
  with `isolation:isolate` on the ring + `z-index:1` on the center text, else the
  text renders UNDER the conic gradient and is unreadable.
- Variance hue must be STATUS-AWARE: a money gap (leak/unconfirmed) tinted
  red/gold, NEVER green — a "+32%" leak shown green reads as good news (wrong).
QA all states by seeding a TEMP demo tenant covering every status (leak w/
independent feed, unconfirmed w/ GMP-only, ok, needs-data), screenshot desktop +
390px mobile, then DELETE the demo tenant. (Inverter model needs `serial` NOT
NULL — but skip seeding inverters: the independent-feed gate keys off
DailyGeneration `source`, not inverter rows.)
WEEKLY CLIENT DIGEST (BUILT this session, commit 6d2e09e): scheduler job
`deliver_weekly_audit_digest` (CronTrigger Mon 13:00 UTC, registered in
scheduler.start alongside deliver_weekly). Loops active product=="array_operator"
tenants, runs `_build_audit_for_tenant` (same reconcile_array roll-up as the
route), emails the owner an AO-skinned digest (`notify._send_via_resend(...,
product="array_operator")` + `email_skin.render_email_skin`): dollars flagged
headline, flagged-array table, coverage line, Audit CTA. HONEST by construction —
sends an "all clear" when nothing flagged, never invents a leak, SKIPS owners
with no bills (have_settlement==0) so they get no noise; per-tenant try/except so
one fleet can't stall the batch. Recipient = `tenant.contact_email`. Verify by
seeding a temp demo tenant + real Resend send (local DB has no RESEND_API_KEY, so
monkeypatch `notify.RESEND_API_KEY` from ~/.hermes/secrets/resend_api_key to
actually fire), then clean up. Cron timing is registered-in-code → confirm via
`railway ssh ... python -c "from api.scheduler import deliver_weekly_audit_digest"`
on the deployed image, NOT the migrate log.

## Test seeding gotcha (shared sqlite, no per-test isolation)
Seeding for recon tests: `Tenant` needs `tenant_key`; `Bill` and
`GmpDailyGeneration` and `DailyGeneration` all need `tenant_id`;
`GmpDailyGeneration` also needs `account_number` + `array_id`. Use an autouse
teardown that deletes by tenant_id (pattern in
`tests/test_reconciliation_gmp_feed.py`) so rows don't leak into other agents'
global assertions.
