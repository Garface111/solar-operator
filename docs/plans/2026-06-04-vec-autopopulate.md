# Agent G — VEC autopopulate

## Goal
Currently autopopulate works for GMP only. When a VEC capture lands at
`/v1/sync`, the bills persist (already wired in commit 3531fd3) but the
extension-side capture does NOT auto-create Arrays linked to a matching
Client — because the autopop branch matches by `Client.gmp_email` /
`Client.gmp_username` only.

Generalize it so VEC works the same way: operator pastes the client's
VEC login email on the Client edit form, toggles `vec_autopopulate=true`,
and the next time the extension scrapes VEC, the arrays land linked
to that Client.

## Scope — ONLY these files
- `api/models.py` — add `vec_email`, `vec_username`, `vec_autopopulate`,
  `vec_last_sync_at` columns on Client (mirror the GMP triple + sync timestamp)
- `api/migrate.py` — idempotent ALTER TABLE additions for the four columns
- `api/app.py` — generalize the autopop branch in `/v1/sync` to:
  - For GMP captures: match by `gmp_email` OR `gmp_username` (current behavior,
    preserved exactly)
  - For VEC captures: match by `vec_email` OR `vec_username`
  - Refactor the two paths to share code where clean; don't break GMP behavior
- `api/account.py` — expose the new fields on the Client API:
  - GET /v1/account/clients should include them in the response
  - PUT/PATCH for client should accept them as updatable
- `web/app/src/lib/api.ts` — TypeScript types for the new fields
- `web/app/src/components/ClientCard.tsx` or wherever the GMP login fields
  live — add equivalent VEC fields side-by-side (or in a tabbed/grouped section)
- `web/app/src/components/AddClientModal.tsx` — add the new fields to the form

## Do NOT touch
- `extension/` — extension already POSTs provider="vec" correctly
- `api/adapters/` — adapter parsing already correct
- `api/delivery.py`, `api/notify.py`, `api/email_templates.py` — out of scope
- `web/onboarding/` — separate funnel
- Stripe code
- Other agents are running on:
  - `web/app/src/screens/DashboardLayout.tsx` — dashboard walkthrough
  - Most `lib/api.ts` consumers + several cards — auto-refresh hooks
  COORDINATE: this agent edits `lib/api.ts` to add VEC fields to the type;
  the auto-refresh agent only edits `lib/api.ts` if needed for refetch
  helpers. Keep your changes additive (new optional fields) so they merge.

## Implementation notes

### Generalized autopop dispatch
The current /v1/sync autopop block hardcodes:
```python
match_terms = []
if user_email: match_terms.append(func.lower(Client.gmp_email) == user_email)
if user_username: match_terms.append(func.lower(Client.gmp_username) == user_username)
clients = ... .where(Client.gmp_autopopulate.is_(True), or_(*match_terms))
```

Refactor to a small dispatch:
```python
PROVIDER_AUTOPOP_FIELDS = {
    "gmp": {
        "email": Client.gmp_email,
        "username": Client.gmp_username,
        "autopopulate": Client.gmp_autopopulate,
        "last_sync": Client.gmp_last_sync_at,
        "bill_offset_months": 1,  # GMP default
    },
    "vec": {
        "email": Client.vec_email,
        "username": Client.vec_username,
        "autopopulate": Client.vec_autopopulate,
        "last_sync": Client.vec_last_sync_at,
        "bill_offset_months": 0,  # VEC bills same-month
    },
}
```
Use the right column set based on `provider`. GMP behavior MUST be byte-
identical (existing tests must still pass).

### Migration
Four new columns, all nullable / default. ALTER TABLE clients ADD COLUMN ...
Idempotent (check column_exists first, same pattern as the recent
`arrays.excluded` migration).

### Tests
Add a `tests/test_vec_autopop.py` mirroring `tests/test_autopop.py` if such a
file exists (search for it). If GMP doesn't have explicit autopop tests yet,
add minimal coverage for VEC alone.

## Deliverables
- Branch `agent/vec-autopopulate`
- 5-line summary: (1) files touched, (2) test results (existing GMP tests
  must still pass — verify and report counts), (3) migration steps for
  Railway, (4) any UI compromises (e.g. cramming VEC fields below GMP vs.
  side-by-side), (5) confidence 1-10
- Run ./build_app.sh after touching web/app/src/

## Verification before declaring done
1. Run pytest — existing tests stay green
2. Manually simulate a VEC POST to /v1/sync with a Client pre-configured with
   matching vec_email + vec_autopopulate=true; confirm Arrays get created
3. Migration is idempotent on re-run
