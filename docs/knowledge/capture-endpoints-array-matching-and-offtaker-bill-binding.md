# Capture endpoints: array matching, the soft-delete 500, and offtaker↔GMP-bill binding

Covers the **whole class** of extension-capture → array/account/bill persistence bugs and
the offtaker-invoice-from-utility-bill feature. Multiple capture endpoints share these
patterns; a fix in one almost always belongs in the others too.

## The capture endpoints (all share the same matching pitfalls)
- `POST /v1/sync` (app.py) — GMP/SmartHub session + **Bill** ingest (creates UtilityAccount + Bill).
- `POST /v1/array-owners/inverter-capture` (array_owners.py) — Fronius/SMA/Chint per-inverter rows.
- `POST /v1/array-owners/utility-meter-capture` (array_owners.py `_persist_meter_accounts`) —
  GMP/VEC/WEC generation read off the utility meter.

> Hunting this class proactively + stale-test triage + the UI dataviz pattern live in
> `references/bug-hunt-pass-stale-test-triage-and-svg-dataviz.md`.

## PITFALL 1 — `uq_array_per_tenant` soft-delete name-collision → HTTP 500 (recurring class)
`uq_array_per_tenant` spans `(tenant_id, name)` with **no `deleted_at` predicate**, so a
soft-deleted array STILL reserves its name. Any capture path that builds a "match-by-name"
map from **non-deleted arrays only** will, on a re-capture whose name matches a deleted array,
try to INSERT a colliding name → `psycopg2.errors.UniqueViolation` → unhandled `IntegrityError`
→ 500. User-visible as e.g. "couldn't grab your GMP account http 500".

FIX (apply to EVERY capture path that creates arrays):
```python
existing = db.execute(select(Array).where(Array.tenant_id == tenant.id)).scalars().all()  # NO deleted_at filter
by_name = {a.name.strip().lower(): a for a in existing}
...
arr = by_acct_number.get(acct) or by_name.get(name.lower())
if arr is None:
    arr = Array(...); db.add(arr); db.flush()
elif arr.deleted_at is not None:
    arr.deleted_at = None   # REVIVE, don't collide
```
This bit Fronius inverter-capture first, then utility-meter-capture, then **`solaredge_connect_account`
+ `locus_connect_account`** (the "connect ALL my sites" flows). Those two don't revive — they
DISAMBIGUATE (`name (site_id)`) — but the collision guard `if name.lower() in names_lower` was built
from LIVE arrays only, so a site colliding with a soft-deleted array's name slipped past and the
INSERT 500'd. FIX there: build a SEPARATE `all_names_lower` set from ALL names (no `deleted_at`
filter) purely for the guard, keep the reuse maps (`by_name`/`by_site_id`/`arr_by_id`) on live arrays.
When you fix it in one, grep ALL capture/connect endpoints for `deleted_at.is_(None)` feeding an
array name-map that precedes an INSERT. Regression test pattern: create a soft-deleted array, then
capture/connect a same-named site → must 200 (revive or disambiguate), not 500. Verify the test
reproduces the `IntegrityError` against the OLD code (git stash the fix) before trusting it.

## PITFALL 2 — Fronius "inverters in the wrong array" = name-only site→array match
Fronius/extension capture matched a captured site to its array **by mutable name**. A rename
(in AO or a portal/AO name mismatch) made the next capture spawn a **phantom duplicate array**
and route new inverters/generation there, splitting devices from siblings.
FIX: anchor on the **stable vendor site id** (Fronius PvSystemId), persisted on every inverter
as `source_site_id` (tied to `source_array_id`). Match priority: **site_id → name → create**.
First-ever capture (no inverter rows yet) still falls back to name. Existing inverters keep
their owner `array_id` on re-sync ("NEVER clobber owner array_id/position").

## PITFALL 3 — "connected ≠ linkable": capture wrote only DailyGeneration
The offtaker dropdown (`GET /v1/array-operator/billing/utility-accounts`) lists **UtilityAccount**
rows and bills from **Bill.kwh_generated**. `_persist_meter_accounts` originally wrote only
Array + **DailyGeneration** — so a GMP account showed "connected" (generation landed) yet the
dropdown stayed empty and there was no bill to invoice from. FIX: in the capture path, for each
account WITH generation, **upsert a UtilityAccount** (idempotent on tenant+provider+account_number,
linked to the array) AND, when the GMP `summary` carries a billing period + generation, **upsert
a Bill** (`kwh_generated` from `parse_usage_summary`, climbs-only per period_end, one Bill/period).
Lesson: a "capture succeeded" 200 is not proof the feature works — verify the exact rows the
consuming UI reads actually exist.

## OFFTAKER ↔ UTILITY-BILL model (Ford's hard rule)
Offtaker invoices are computed **EXCLUSIVELY from the utility's paper bills**
(`Bill.kwh_generated` for the bound GMP account) — never vendor/inverter telemetry, never GMP
hourly-interval data, never DailyGeneration, and **no fallback**. If no bill covers the period,
delivery **skips** ("waiting on the utility bill"), never fabricates.
Implementation: `BillingReportSubscription.utility_account_id` (nullable, back-compat) binds an
offtaker to a GMP UtilityAccount; `delivery._utility_bill_period_kwh()` reads that account's latest
Bill; `build_manual_match` takes a top-priority utility-bill branch (`kwh_source="utility_bill"`,
flag `has_utility_bill`); `deliver_subscription` skips when `has_utility_bill is False`.
UI: add-offtaker form + setup wizard select the **GMP utility bill** (not an array); both POST
`utility_account_id`. A "Link GMP utility bills" button (Reports tab + a dedicated wizard step)
launches `window.__aoConnectGmp()` (the existing connect flow). Picker auto-refreshes by wrapping
`window.__aoRefreshGmpGate`. Caveat: a Bill is created only if the GMP capture payload includes the
billing-period summary; daily-only payloads give a linkable account but `has_bill=false` until a
summary lands → next fix would be on the extension's GMP scrape, not the backend.

## DIAGNOSIS DISCIPLINE (Ford clamped down on interactive prod probing)
- Do NOT curl/HTTP MC/AO prod endpoints interactively — Ford blocks it (incl. read-only GETs).
- DO read the real traceback from **`railway logs`** and inspect data via **`railway ssh ... python`**
  (read-only SQL) — these are allowed and are the fastest path to the actual error. The GMP 500 was
  found in one `railway logs | grep -iE "500|traceback"` pass, not by guessing.
- Verify a route is healthy by curling it for **401/422 (loads) vs 500 (broken)** after deploy.
- Empty-dropdown / "connected but nothing shows" is almost always a DATA-state mismatch (the rows
  the UI reads don't exist) — check prod DB counts per tenant FIRST before touching rendering code.

## AO tenant reality (recurring fixture truth)
AO tenants (SolarEdge/SMA/Chint owners, the Live Demo `ten_a554c8e7a08f8cfa`) are inverter-vendor
based and start with **zero utility accounts/bills**. GMP bills exist in volume only on NEPOOL
tenants. So an empty offtaker dropdown on a fresh AO tenant is correct until GMP is connected.
