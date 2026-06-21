# AO fleet mutations — soft-delete / restore / undoable across 4 layers

How to add an owner mutation (delete an inverter, delete an array, future moves)
to the Array Operator fleet, end to end. Built this session by mirroring the
existing "Delete array" right-click into a new "Delete inverter". The value is the
4-LAYER pattern — get all four or the feature half-works.

## The 4 layers (find the sibling, copy it exactly)
The cleanest way to add a fleet mutation is to find the analogous existing one and
mirror it. Delete-inverter mirrored delete-array at every layer:

1. **Core mutator** — `api/inverter_fleet.py`. Add `delete_inverter(db, tenant,
   inverter_id)` + `restore_inverter(...)`. SOFT-delete ONLY (set `deleted_at`,
   never `db.delete`) so it vanishes from `build_fleet_tree` (which filters
   `deleted_at.is_(None)`) and an undo can revive it. Ownership-checked: `iv.tenant_id
   != tenant.id` → raise `FleetError` (route turns it into 404, so a cross-tenant
   id leaks nothing). Idempotent: an already-deleted row is treated as not-found.
   AO billing is per-kWh metered (NOT per-array/per-inverter) so these mutators
   must NOT touch Stripe — unlike `api.account.delete_array` for operator clients.

2. **API routes** — `api/array_owners.py`. `@router.delete("/v1/array-owners/
   inverters/{id}")` + `@router.post(".../{id}/restore")`. Dual-auth via
   `_tenant_from_bearer`; call `require_not_demo(tenant)` so the shared read-only
   DEMO tenant gets 403; wrap the mutator call, `except FleetError: raise
   HTTPException(404, ...)`. Verify on prod by hitting the live URL noauth → expect
   401 (registered + guarded), NOT 404 (route not deployed) or 500.

3. **FleetStore mutator** — `array-operator/public/fleet-store.js`. Add
   `deleteInverter(invId)`: optimistic local removal + `apiDelete(...)` (then
   `refetch()`), recorded as an UNDOABLE command via `pushHistory({undo, redo})` —
   undo re-inserts locally at the old index AND calls the restore endpoint; redo
   re-deletes. Single-inverter delete is NOT a structural history barrier (it
   inverts exactly by stable id), unlike createArray/deleteArray which call
   `clearHistory()`. EXPORT the new fn in the public-API `return {...}` block at
   the bottom or the UI can't call it.

4. **Sandbox UI** — `array-operator/public/sandbox.js`. The right-click handler
   already exists for arrays; extend it. CHECK THE MORE-SPECIFIC TARGET FIRST: an
   `.sb-inv` card lives INSIDE an `.sb-col`, so test `closest(".sb-inv")` before
   `closest(".sb-col")` or the inverter right-click opens the array menu. Reuse
   the SAME `.sb-ctxmenu` machinery (one menu on `<body>`, dismiss on outside
   click/Escape/scroll/another contextmenu) — copy `showArrayCtxMenu` →
   `showInvCtxMenu`, just swap the label + `FleetStore.deleteInverter`. The
   `.sb-ctxmenu` CSS already exists in `styles.css` (fixed, bordered, gradient),
   so no new CSS.

## Tests + QA
- Backend: mirror `tests/test_array_owners_delete.py` → a new
  `tests/test_array_owners_inverter_delete.py` covering: soft-delete (row still
  EXISTS, deleted_at set), sibling + parent array untouched, fleet-tree drops the
  inverter but keeps the array, restore roundtrip, cross-tenant 404, demo 403,
  idempotent 404, unauth 401. RUN WITH A SAFE DB: `DATABASE_URL=sqlite:///./test.db
  DATABASE_PUBLIC_URL=sqlite:///./test.db python -m pytest ...` — NEVER inherit a
  prod-pointing env (the MC suite landmine class).
- Frontend: `node --check` both JS files. Visual QA against the demo fleet with
  Playwright (`array-operator` has playwright; use `import {chromium}` in a `.mjs`,
  NOT `require`). Right-click an `.sb-inv`, assert `.sb-ctxmenu` appears with the
  expected label, screenshot + vision_analyze, confirm zero console errors. The
  demo menu renders against dark canvas exactly like the existing array menu.

## Deploy
Backend `git push origin HEAD:main` (Railway, no migration — soft-delete adds no
column). AO frontend MANUAL `python3 scripts/netlify_api_deploy.py`. Commit each
repo separately, staging ONLY your files (shared cron-trap tree). Verify the live
bundle: `curl arrayoperator.com/fleet-store.js | grep -c deleteInverter`.
