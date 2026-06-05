# Merge Two Arrays — One Source of Truth

## Why
When the autopop creates duplicates (e.g. a client's same physical array
shows up as two entries because the GMP bill has two segments, or after
manual + automatic creation collide), the operator currently has to
delete one — losing its Bills and history. They need to **merge** instead:
keep one array, pull all of the other's data into it, delete the empty
source.

## Scope
- NEW backend endpoint: `POST /v1/account/arrays/{target_id}/merge`
  body: `{"source_array_id": <int>}` → reparents UtilityAccounts and
  Bills from source onto target, then soft-deletes source. Returns
  the updated target Array.
- Backend schema/model: NO schema change needed.
- NEW frontend UX in `web/app/src/components/ArrayList.tsx`:
  - When exactly 2 arrays are selected via the existing select mode,
    show a "Merge 2 arrays" button in the bulk-action footer
    (sibling to the existing "Delete N arrays" button).
  - Confirm modal:
    - Show both arrays side-by-side: name, NEPOOL ID, bill_offset_months,
      array_count of UtilityAccounts, total Bills.
    - Radio: "Which array should be kept?" — defaults to whichever has
      a NEPOOL ID; if both have one, default to the one with more bills.
    - Warn LOUDLY if `bill_offset_months` differs (Bruce's Starlake = 0
      vs others = 1 — merging across this boundary is dangerous).
    - Warn if both have NEPOOL IDs and they differ — the chosen one wins.
    - Confirm button: "Merge into [kept name]".
- After merge: refresh ArrayList, toast "Merged X bills + Y accounts
  from [source name] into [target name]".
- DO NOT add an undo for merge — it's a structural operation; instead,
  surface the merge in an audit log if one exists.

## Tasks

### Task 1 — Backend
- Add `POST /v1/account/arrays/{target_id}/merge` in `api/account.py`.
- Validate: both arrays exist, belong to same tenant, source != target,
  neither is soft-deleted.
- In a single transaction:
  - `UPDATE utility_accounts SET array_id=<target> WHERE array_id=<source>`
  - `UPDATE bills SET array_id=<target> WHERE array_id=<source>`
  - Soft-delete source: `source.deleted_at = utcnow()` (or whatever the
    existing soft-delete pattern is — check the codebase).
  - DO NOT touch target.nepool_gis_id or bill_offset_months — operator
    chose target on the frontend with that knowledge.
- Return the updated target Array as JSON.

### Task 2 — Tests
`tests/test_array_merge.py`:
1. Merging A+B reparents A's UtilityAccounts to B.
2. Merging A+B reparents A's Bills to B.
3. After merge, A is soft-deleted (excluded from /v1/account/clients/.../arrays).
4. Bill count of B = old(A) + old(B).
5. 404 when target or source not found, or different tenant.
6. 400 when source == target.

### Task 3 — Frontend
- Edit `ArrayList.tsx` to:
  - Compute `canMerge = selectedIds.size === 2`.
  - Render new "Merge 2 arrays" button alongside delete button when
    `canMerge` is true.
  - Open new `MergeArraysModal.tsx`.
- New file `web/app/src/components/MergeArraysModal.tsx`:
  - Reads both selected arrays from props.
  - Side-by-side card layout.
  - Radio for kept array, defaulting smartly.
  - Warnings for offset mismatch + NEPOOL conflict.
  - Calls `mergeArrays(target, source)` in lib/api.ts.
- Add `mergeArrays` typed wrapper in `web/app/src/lib/api.ts`.

### Task 4 — Build + verify
- `pytest tests/test_array_merge.py -v` green.
- `pytest tests/` all green.
- `./build_app.sh` and commit api/app_dist/.
- Commit per task. Do NOT push. 5-line summary.

## Constraints
- DO NOT touch GMCS writer.
- DO NOT change Array/UtilityAccount/Bill models.
- Use the existing soft-delete convention (search ArrayList delete flow
  to find it — likely `deleted_at: datetime | None`).
- Match the cream/emerald/wood Solarpunk visual language.
- Type hints required (Python 3.11+ and TS strict).
