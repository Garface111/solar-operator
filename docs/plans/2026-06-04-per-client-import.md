# Per-client NEPOOL import + walkthrough integration

## Goal

Two coordinated additions to make the NEPOOL-import flow feel scoped and
guided:

1. **Per-client "Import NEPOOL IDs" button** on each expanded ClientCard.
   Scopes the matcher to that client's arrays only, so a spreadsheet with
   just that client's array names + NEPOOL IDs propagates IDs without
   leaking matches to other clients.

2. **Master "Import spreadsheet" button** at the top of the Clients tab
   stays as it is today — that's the "I have a big GMCS-shaped master
   sheet covering many clients/arrays, figure it out" entry point.

3. **Walkthrough step**: between the existing "Toggle auto-populate ON"
   step and the "Go log in" step, add a new step that highlights the
   per-client Import button and explains: "If your client already has a
   sheet with NEPOOL IDs, drag it here to auto-fill them — no typing
   required."

## Hard rule: stay in these files

ONLY edit:
- `api/nepool_assign.py` — extend `/v1/account/nepool/preview` to accept
  an optional `client_id` query param. When present, scope the existing
  arrays query to that client and exclude the rest from matching.
- `web/app/src/lib/api.ts` — add a helper to call the preview endpoint
  with a client_id, mirroring whatever helper currently exists.
- `web/app/src/components/ClientCard.tsx` — add a small "Import NEPOOL
  IDs" button in the expanded body (near the arrays list or autopop
  toggle). Tag it `data-tour-step="5"` for the walkthrough.
- `web/app/src/components/ImportSpreadsheetModal.tsx` — accept an
  optional `clientId` + `clientName` prop. When set, pass `client_id` to
  the preview API call, change the modal title to "Import NEPOOL IDs for
  <Client Name>", and only show that client's arrays in any
  available-arrays / manual-assign UI.
- `web/app/src/components/WalkthroughOverlay.tsx` — add a 5th step
  (between the current step 4 "Toggle auto-populate ON" and the existing
  step 5 "Go log in"). New step anchors to `[data-tour-step="5"]`
  (the per-client Import button). Use `waitForClick: false` for this one
  with a "Next →" button — clicking the Import button would open a
  modal which would break the tour flow; just explain it and let the
  operator click Next.

## Do NOT touch

- `web/app/src/components/ClientsSection.tsx` — the master Import button
  stays. Do NOT change its behavior.
- Stripe code
- `extension/`, `api/adapters/`, `api/writers/`
- Other dashboard cards
- The onboarding SPA (`web/onboarding/`)

## Backend contract change

The preview endpoint already returns an `available_arrays` list scoped
to all of the tenant's unassigned arrays. With `client_id`:

```
GET/POST /v1/account/nepool/preview?client_id=42
```

- Loads `existing_arrays` filtered by `Array.client_id == client_id`
  AND the existing tenant + deleted_at filters
- `matched_array_ids` and `available_arrays` naturally fall out scoped
  to that client because they iterate `existing_arrays`
- `unmatched_pairs` is unchanged semantically — these are extracted
  pairs that didn't match any of this client's arrays, which is
  exactly the right behavior for a per-client import (a row from
  another client's section of the spreadsheet would just go unmatched
  rather than land on the wrong client)
- If `client_id` is not a UUID/int that maps to a Client owned by the
  tenant, return 404

Validate the client_id belongs to the tenant before scoping the query.

## Frontend wiring

The existing modal reads as a "find what's missing across everything"
flow. When `clientId` is set:
- Title: `Import NEPOOL IDs for <ClientName>`
- Helper text mentions this scopes to just that client's arrays
- Calls preview with `?client_id=<id>` so the proposals + available
  arrays are scoped server-side
- The commit step is unchanged — it takes array_id → nepool_gis_id pairs
  and those array_ids are already client-scoped from the preview
  response

Keep the existing modal's overall UX intact. Don't restructure it; just
parameterize it.

## Walkthrough step ordering

After this change, STEPS in WalkthroughOverlay should be:

```
1. Intro (anchor: null)            -- "Get started →" button
2. Click a client to expand (#2)   -- waitForClick
3. Enter utility login (#3)        -- waitForClick
4. Toggle auto-populate ON (#4)    -- waitForClick
5. NEW: Import NEPOOL IDs (#5)     -- "Next →" button
6. Go log in (anchor: null)        -- "Open GMP" CTA
7. You're all set (anchor: null)   -- "Done"
```

Bump the STEPS count display logic accordingly (it uses
`STEPS.length` already; should auto-adjust).

Body copy for the new step:

> Title: "Bulk-import NEPOOL IDs"
> Body: "If your client already has a spreadsheet with their array
>        names and NEPOOL-GIS IDs, click this Import button and drop
>        the file in. We'll auto-fill the IDs for every array we can
>        match — no typing required. You can also do this from the
>        master Import button at the top of the page when you've got
>        a master sheet covering multiple clients."

## Deliverable

- Branch `agent/per-client-import`
- 5-line summary: (1) files touched, (2) build clean? tests pass?
  (3) any backend contract surprises (e.g. existing callers of the
  preview endpoint that pass extra args), (4) any UI compromise, (5)
  confidence 1-10
- Run `./build_app.sh` after web/app edits
- Push to origin

## Verification

1. From the Clients tab, expand a client, click the new "Import NEPOOL
   IDs" button — modal opens scoped to that client only
2. Drop a sample sheet → preview shows matches against that client's
   arrays only
3. Commit → only those arrays get NEPOOL IDs
4. Master Import button at the top still works for cross-client sheets
5. Open the walkthrough — step 5 highlights the per-client Import
   button on the expanded ClientCard
