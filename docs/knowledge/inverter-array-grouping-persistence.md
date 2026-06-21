# Inverters "moving / jumping between arrays" — owner-grouping persistence

This is a DISTINCT class from live-inverter-feed-debugging.md (that one is about a
null/stale live POWER reading). This is about an inverter's ARRAY MEMBERSHIP
(owner grouping) drifting — Ford reports it as "inverters are moving around
between arrays and not being connected to the array they should be." There are
TWO independent root causes; check BOTH, they compound.

Owner grouping lives in `Inverter.array_id` (mutable, owner's drag). The stable
discovery anchor is `Inverter.source_array_id` + `Inverter.source_site_id`
(vendor PvSystemId). Capture/sync MUST refresh telemetry/source pointers but NEVER
clobber `array_id`/`position` — that arrangement is sacred.

──────────────────────────────────────────────────────────────────────────────
## CAUSE 1 (client) — background refetch clobbers an optimistic drag (RACE)

Where: `/root/array-operator/public/fleet-store.js`. `reassignInverter()` does an
OPTIMISTIC local move → `notify()` (canvas updates instantly) → fires the
`/v1/array-owners/inverters/reassign` POST in the background → `.then(refetch())`.

The trap: the 5-min auto-refresh + the `visibilitychange` refetch (both added for
the source-offline banner — see offtaker-reports-and-sandbox-source-routing.md §3)
had NO awareness of in-flight writes. A background `refetch()` landing in the
~100–500 ms window AFTER a drop but BEFORE the reassign POST persisted re-ingested
STALE server state and snapped the inverter back to its old array. Rapid
successive drags hit the same race. `_userBusy()` (drag-class check) does NOT cover
this window because the drag class is gone by the time the POST is in flight.

FIX (shipped): in-flight write tracking.
- `_pendingWrites` counter; `_trackWrite(p)` wraps BOTH `apiPost` and `apiDelete`
  (increment on start, decrement on settle, in both `.then` and `.catch`).
- `refetch()` bails while `_pendingWrites > 0` (sets `_refetchQueued = true`).
- When the last write settles, if a refetch was queued, run it once ~150 ms later
  so we still converge on authoritative server state.
This fixes the race for drags, the 5-min auto-refresh, AND tab-refocus — while
KEEPING the banner-auto-clear feature. Do NOT "fix" it by disabling auto-refresh;
the banner needs it. Verify live: `curl .../fleet-store.js | grep _pendingWrites`.

GENERAL LESSON: any optimistic-mutation + background-refetch UI needs an in-flight
write guard, or the refetch will clobber the optimistic state. When you ADD a
periodic/auto refetch to a store that has optimistic writes, add this guard in the
SAME change.

──────────────────────────────────────────────────────────────────────────────
## CAUSE 2 (server) — Fronius/extension capture matched site→array by NAME

Where: `api/array_owners.py` `inverter_capture()` (the
`/v1/array-owners/inverter-capture` endpoint, vendors fronius/chint/sma).

The trap: each captured `CaptureSite` was matched to its `Array` by NAME only
(`by_name[site_name.lower()]`). The site name is MUTABLE — the owner can rename
the array in AO, and the portal name can differ. So after a rename the next
capture found no name match → CREATED A PHANTOM DUPLICATE array → routed that
site's `DailyGeneration` + any newly-appearing inverter into the phantom, while
the existing inverters (matched globally by `(vendor, serial)`, array_id
preserved) stayed put. Result: a device split from its data and its siblings.
Ford reproduced it specifically on Fronius.

Note: existing inverters were ALWAYS safe — the per-serial upsert explicitly
"NEVER clobber owner array_id/position." The damage was only to NEW inverters and
the site's daily-gen routing after a name drift.

FIX (shipped): anchor the site→array match on the STABLE site id, not the name.
- Build `by_site_id: {source_site_id -> Array}` from existing inverter rows of
  this vendor (`source_site_id` → `source_array_id` or `array_id`).
- Match priority per captured site: (1) `by_site_id[site.site_id]` (rename-proof)
  → (2) `by_name` → (3) create. Remember new bindings in `by_site_id` within the
  batch so a same-name-different-site collision can't steal it.
- First-ever capture (no inverter rows yet) still falls back to name — correct.
No DB migration (no new column; reuses existing `source_site_id`).

TESTS (`tests/test_array_owners.py`):
- `test_inverter_capture_rebinds_by_site_id_after_rename` — rename → re-capture
  stays ONE array, no phantom, all inverters keep their array_id.
- `test_inverter_capture_preserves_manual_reassignment_on_recapture` — a dragged
  inverter stays put across re-captures.
PITFALL when adding tests here: `test_fleet_tree_renders_fronius_comb` has a long
body; inserting new `def test_...` mid-file split its trailing `col[...]`
assertions. Append new tests AFTER a function's full body, not between its last
two lines.

──────────────────────────────────────────────────────────────────────────────
## CAUSE 3 (server) — capture 500s with UniqueViolation on uq_array_per_tenant (soft-deleted name collision)

Symptom Ford saw: clicking "Link GMP utility bills" → toast "couldn't grab your
GMP account · HTTP 500". This is a THIRD array-creation endpoint hitting the same
root-cause family. There are now THREE capture/sync paths that create Arrays and
ALL must guard the same way:
  • `inverter_capture()`            — /v1/array-owners/inverter-capture (fronius/sma/chint)
  • `_persist_meter_accounts()`     — /v1/array-owners/utility-meter-capture (gmp/vec/wec)
  • the `/v1/sync` bill-capture path (app.py) — GMP bills

THE TRAP: `uq_array_per_tenant` is `UniqueConstraint(tenant_id, name)` with NO
`deleted_at` predicate — so a SOFT-DELETED array still RESERVES its name. Any
capture path that builds its "reuse vs create" name-map from NON-deleted arrays
only (`...where(Array.deleted_at.is_(None))`) will MISS a soft-deleted same-name
array, try to INSERT a colliding name, and Postgres raises
`psycopg2.errors.UniqueViolation` → unhandled `IntegrityError` → HTTP 500.

FIX (shipped, mirrors the Fronius path's older fix): in EVERY array-creating
capture path —
  1. Build the name-map from ALL arrays of the tenant (drop the
     `deleted_at.is_(None)` filter): `select(Array).where(Array.tenant_id==t.id)`.
  2. On reuse, if the matched array `deleted_at is not None`, REVIVE it
     (`arr.deleted_at = None`) instead of inserting — generation is flowing again.
No migration. `_persist_meter_accounts` (api/array_owners.py ~2896) was the one
fixed this session; `inverter_capture` and `/v1/sync` already do this. When you
add a NEW capture/array-creation path, include all-arrays + revive from the start.

DIAGNOSIS METHOD (how I found it without prod HTTP probing): READ OUR OWN RAILWAY
LOGS for the traceback — `railway logs 2>&1 | grep -iE "500|traceback|UniqueViolation|/v1/..."`.
The log named the EXACT endpoint + the `uq_array_per_tenant` constraint, which is
faster and cleaner than guessing from the user-facing message ("couldn't grab your
GMP account" came from the extension, NOT the endpoint name). Reading our logs is
allowed; interactive prod HTTP probing is not (Ford clamped that down).

TEST (`tests/test_utility_meter_capture_match.py`
`test_capture_revives_soft_deleted_array_instead_of_500`): seed a SOFT-DELETED
array whose name == the capture-derived name (nickname-less GMP acct → "GMP <acct>"),
POST the capture, assert 200 (not 500), same row revived (deleted_at cleared), no
duplicate. PROVE the test catches the bug: `git stash push api/array_owners.py` →
run the test → it FAILS (reproduces the 500) → `git stash pop`. A regression test
that doesn't fail on the old code isn't proving anything.

──────────────────────────────────────────────────────────────────────────────
## RELATED — offtaker invoices need GMP *Bills*; empty bill-picker ≠ a bug

When the offtaker "Which GMP utility bill?" picker is empty, FIRST check whether
the AO tenant has ANY GMP data before assuming the endpoint is broken. AO tenants
are inverter-vendor based (SolarEdge/SMA/Chint) and typically have ZERO GMP
for that product. Diagnose with a
read-only railway query: count `utility_accounts WHERE provider='gmp'` and
`bills ... kwh_generated IS NOT NULL` per AO tenant (they'll be 0); GMP data lives
on the NEPOOL tenants. The fix is a CONNECT path, not a pull fix — a "Link GMP
utility bills" button in the Reports tab that calls the existing
`window.__aoConnectGmp()` (opens greenmountainpower.com; the extension captures →
POSTs the bills). NOTE (updated Jun'26): `utility-meter-capture` NOW also upserts
`UtilityAccount` + `Bill` itself (not just DailyGeneration) — see
offtaker-reports-and-sandbox-source-routing.md §1c(A); it no longer depends on
/v1/sync having run for the picker to populate. Offtaker invoices still bill
EXCLUSIVELY from `Bill.kwh_generated`, so an account with a captured account but no
billing-period summary shows "no bill on file yet" until a summary lands. Full
offtaker↔bill build: references/offtaker-reports-and-sandbox-source-routing.md.

──────────────────────────────────────────────────────────────────────────────
## PROD CLEANUP — find + repair already-misplaced inverters (Bruce's LIVE data)

After fixing CAUSE 2, pre-existing phantoms/splits may remain. Detect them
read-only (railway ssh is allowed; interactive prod HTTP probing is NOT without
per-command approval):

```sql
-- site_ids whose inverters are spread across >1 array (a split = likely phantom)
SELECT tenant_id, source_site_id, COUNT(DISTINCT array_id) AS arrays, COUNT(*) AS invs
FROM inverters
WHERE deleted_at IS NULL AND source_site_id IS NOT NULL
GROUP BY tenant_id, source_site_id HAVING COUNT(DISTINCT array_id) > 1;
```

A split is AMBIGUOUS: it can be the bug OR a deliberate owner drag. Per Ford's
deletion-safety rule, do NOT auto-merge — inspect (array names, all 12 invs'
`source_array_id`, utility_accts, daily_gen per array), SHOW Ford the exact rows,
and only move after explicit confirmation. When moving, touch ONLY the specific
inverter ids by id (not a broad UPDATE), place them at `MAX(position)+1` in the
target array to avoid collisions, then re-run the split query to confirm 0
remaining. This session: 2 Fronius Primo inverters were under "Tannery Brook"
though all 12 were discovered under "Waterford" (`source_array_id=1300`); after
Ford's go, moved those 2 by id → Waterford=12, splits=0.
