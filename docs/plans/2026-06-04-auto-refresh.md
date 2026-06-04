# Agent H — Auto-refresh on async events

## Goal
Several "magic" dashboard features only update after a manual page refresh
because the dashboard renders from a one-shot fetch at mount and never
re-checks. Specifically:

1. **Auto-populate**: operator toggles `gmp_autopopulate=true` + opens GMP →
   extension captures → server creates Arrays → dashboard still shows the
   OLD state until refresh. Bruce wants the new arrays to appear
   automatically, ideally within a few seconds.
2. **Spreadsheet import**: operator drags an Excel file → backend assigns
   NEPOOL IDs → dashboard still shows un-assigned state until refresh.
3. **Bill capture from extension**: operator logs into GMP/VEC → extension
   POSTs bills → dashboard doesn't reflect new "last captured" timestamp.

## Approach: lightweight trigger-based polling

Do NOT add SSE / WebSockets in this agent. Just polling, scoped to the
window after a known trigger fires.

Pattern:
```ts
// After a user action that triggers async server work, fire a poller
function pollUntilChanged<T>(
  fetcher: () => Promise<T>,
  isChanged: (prev: T, next: T) => boolean,
  opts: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<T | null> { ... }
```
Call it after:
- Toggling `gmp_autopopulate` or `vec_autopopulate` ON
- Pressing "Import spreadsheet" (after upload completes)
- Clicking "I've installed it" on onboarding (if applicable — but the
  onboarding flow is out of scope; this is dashboard-only)

Poll `/v1/account/clients` (or the same endpoint the dashboard already uses)
every 2s for up to 30s. As soon as the response differs from the last-known
state in a meaningful way (new Array, new Bill, new NEPOOL ID populated),
trigger a re-render via whatever state mechanism the dashboard uses
(SWR / React Query / hand-rolled hook — check what's there first).

If no change after 30s, give up silently (don't error-toast — the change
may genuinely take longer or never come).

## Scope — ONLY these files
- New: `web/app/src/lib/poller.ts` — generic `pollUntilChanged` helper
- Edit: `web/app/src/lib/api.ts` — ONLY to add small refetch helpers if
  needed; do NOT restructure existing exports
- Edit: the components that perform the triggering actions:
  - `web/app/src/components/ClientCard.tsx` — for the autopopulate toggle
  - `web/app/src/components/ImportSpreadsheetModal.tsx` — for the import
  - `web/app/src/components/ClientsSection.tsx` — if that's where the
    parent re-fetch lives
  - Wherever the autopopulate toggle for `gmp_autopopulate` is wired
- Possibly: `web/app/src/screens/DashboardLayout.tsx` only if you need to
  hoist a refetch handler

## Do NOT touch
- `api/` — this is frontend-only
- `extension/`
- `web/onboarding/`
- Stripe code
- Other agents are running on:
  - `web/app/src/screens/DashboardLayout.tsx` — dashboard walkthrough.
    If you must edit it, ADD a prop or hook without restructuring its
    layout. Coordinate by keeping changes additive.
  - `api/models.py` + most card forms — VEC autopop. They'll add new VEC
    fields to ClientCard/AddClientModal. If you ALSO touch those files,
    LIMIT changes to wiring up the poller — do not rewrite the form
    structure. Use `useEffect`/hook additions, not JSX restructuring.

## Constraints
- No new dependencies
- Plain `fetch` + `setInterval` / `setTimeout` is fine
- The poller must cancel itself on component unmount
- Be conservative: only poll for 30s after a trigger, not forever
- Don't show a spinner the whole 30s — just silently update when change lands

## Verification
- Open dashboard, toggle autopopulate ON, log into GMP in another tab,
  simulate a capture (or POST manually to /v1/sync) → arrays appear without
  refresh within ~4s
- Drag a spreadsheet, watch NEPOOL IDs populate without refresh
- Run `./build_app.sh` after edits

## Deliverable
- Branch `agent/auto-refresh`
- 5-line summary: (1) files touched, (2) verification result, (3) any
  shared-state contention with the other two agents (be specific about
  which files you touched and what kinds of edits), (4) anything Ford should
  know before merge, (5) confidence 1-10
