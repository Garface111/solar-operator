# Session auth, SESSION_SECRET, and the "demo masks real data" bug class

This is the single most recurring bug class in Array Operator. Owners report
their arrays/inverters "keep getting forgotten" or "disappear on refresh," or get
a cryptic "Invalid or inactive tenant key" when connecting. The data is almost
NEVER actually lost — it's a session/auth failure that the frontend masks.

## Root cause (June 2026): SESSION_SECRET was unpinned

- `api/account.py`: sessions are stateless HMAC blobs. `SESSION_TTL_SECONDS = 30d`.
  When `SESSION_SECRET` env var is unset, it derives from `DATABASE_URL`
  (`SESSION_SECRET = sha256(DATABASE_URL)`). The "generated at startup" comment is
  misleading — it's stable ONLY while DATABASE_URL is byte-identical.
- Railway changes DATABASE_URL on Postgres re-provision / credential rotation /
  redeploy events → the signing secret rotates → every issued `so_session` token
  fails `_verify_session` HMAC → backend returns 401 (or 403 via the tenant-key
  fallback path) → owners silently logged out.
- THE FIX (permanent): pin an explicit `SESSION_SECRET` in Railway Variables
  (a fixed `openssl rand -hex 32`). One-time: it logs everyone out once (old
  tokens were signed with the derived secret); after that sessions survive every
  redeploy. This is an ENV VAR change on the LIVE backend — gate it on Ford's
  go-ahead, and warn him about the one-time logout.

## FOURTH masking point — defaultFocusIds() narrowing a real owner (fixed Jun 2026, 148fa44)

Symptom flavor: "forgot ALL his arrays, only remembering ONE of each." NOT the three
demo-fallback points below and NOT data loss — `public/fleet-store.js` `defaultFocusIds()`
narrowed the sandbox to the "worst ~4 flagged" sites on load. That narrowing exists ONLY to
stop the anonymous 100-array SIMULATED demo from opening as 100 columns; it was wrongly
applied to signed-in owners too, hiding most of their real arrays.
FIX (hard invariant): top of `defaultFocusIds()` →
`if(!state.simulated) return state.arrays.map(a => a.id);` so a real owner ALWAYS sees EVERY
array; only the simulated demo keeps the worst-few slice. Audit this WHENEVER "forgetting" is
reported — it's a separate mechanism from the three below.
Proof-of-safety recipe used: query prod DB + `_sign_session(str(tenant.id))` →
TestClient `/v1/array-owners/fleet-tree` returns full count (Bruce's AO owner tenant
`ten_544fd6541eb8405b` "SMA owner" = 9 arrays / 62 inverters, all live, 0 deleted). That
owner tenant is the live AO owner-side test account (distinct from his nepool
`ten_6522da7ac2e1d01d`). Verify the deployed fix with a cold playwright E2E against
arrayoperator.com injecting the session — expect all 9 `.sb-array` cards, 0 JS errors.

## The masking bug (frontend) — fix in THREE places, they drift apart

The backend persists correctly (proven: create_array → build_fleet_tree returns
the array across a fresh DB session; overview returns ALL non-deleted arrays
regardless of generation data). The bug is the live site (/root/array-operator
public/) falling back to DEMO data on ANY auth failure, which reads as "my data
vanished." There are THREE independent copies of this fallback — fixing one does
NOT fix the others. Audit all three whenever this class is reported:

1. `public/app.js` loadDashboard() — overview fetch. On !ok it fell back to
   `inverter-truth.json` (demo). And arrays.length===0 also showed demo.
2. `public/fleet-store.js` load() AND refetch() — fleet-tree fetch. On fail OR
   empty it fell back to `simulateFleet()` (the FAKE 100-array demo fleet). This
   is the "add arrays, refresh, see 100 fake ones" bug.
3. `public/sandbox.js` submitConnect() AND handleCaptureLanded() — the connect /
   extension-capture handlers. Only special-cased 401; a rotated session returns
   403 "Invalid or inactive tenant key" (app.py:465, the tenant-key fallback),
   which fell through to dumping the raw error string at the owner.

### The correct pattern (apply to every owner-facing fetch)
- Treat 401 OR (403 with /tenant key|sign in|session/i in detail/message) as
  EXPIRED SESSION, not a data outage or a credential error.
- On expired session: `localStorage.removeItem("so_session")`, show
  "Your session expired — sign in again. Your existing arrays are safe." with a
  link, and clear the dead token. NEVER render demo.
- Signed-in owners NEVER see the simulated/demo fleet. Empty = honest empty state
  ("nothing connected yet"), not demo. Demo/simulateFleet is for ANONYMOUS
  visitors only (marketing/preview).
- Reserve demo fallback strictly for transient (network/5xx) on a signed-in user
  if you want to avoid a blank page — but prefer an honest empty tree.

## Diagnostic shortcut
When an owner says "data disappeared" / "tenant key error": it's almost certainly
a dead session, not lost data. Have them sign out + back in at
arrayoperator.com/login first — that resolves it 9/10 times. Then verify the
three frontend masking points are auth-aware, and confirm SESSION_SECRET is pinned
(the root cause). Prove persistence with a backend create→fresh-session-read test
before ever touching the DB.

## A FOURTH mechanism — the focus-subset "forgot my arrays / only one of each" bug (Jun 2026)

Not every "it forgot my arrays" report is auth/demo-masking. A distinct flavor —
symptom "only remembering ONE of each / keeps forgetting all his arrays and
inverters" — was `public/fleet-store.js` `defaultFocusIds()` narrowing the sandbox
to only the WORST ~4 FLAGGED sites on load. That narrowing exists ONLY so the
ANONYMOUS 100-array `simulateFleet()` demo doesn't open as 100 columns — but it was
applied to REAL signed-in owners too, so an owner with 9 arrays saw a handful.
Made newly visible by the array-card restructure shipping inverters COLLAPSED by
default (the comb hidden until you click the toggle) — together they read as
"forgot everything, one of each," even though nothing was lost.

THE FIX (permanent, root-cause): in `defaultFocusIds()` guard the narrowing on
`state.simulated` — `if(!state.simulated) return state.arrays.map(a => a.id);` so a
real owner ALWAYS gets EVERY array; only the simulated demo keeps the worst-4 slice.
It routes through all three callers (`focusColumns`, `focusIds`, and the `ingest()`
`if(!state.focus.length) state.focus = defaultFocusIds()` reset). Treat "owner sees
ALL arrays" as a HARD product invariant and leave a code comment locking it.

PROOF HARNESS (not just assertion): load `fleet-store.js` in a Node `vm` context with
minimal window/localStorage/document/fetch stubs, set `localStorage.so_session`, make
`fetch` resolve a fleet-tree with N columns, call `FleetStore.load()`, then assert
`focusIds().length === N` and `focusColumns().columns` carries all inverters; also
regex-assert the simulated branch still slices worst-4. (Vanilla static site, no
build — drive the real IIFE this way instead of a browser.)

## PROVE data safety FIRST with a read-only prod DB probe (do this before any fix or reassurance)

The reassurance "your data is safe" must be EVIDENCE, not faith. Run a read-only
probe against prod and quote the real counts. Recipe (the api package is at
`/app/api` on prod, so RUN FROM `/app`, not `/tmp` — `cd /tmp && python` gives
`ModuleNotFoundError: No module named 'api'`):
  `B64=$(base64 -w0 probe.py); railway ssh --service web "echo $B64 | base64 -d >`
  `/app/_probe.py && cd /app && python _probe.py > /tmp/out.json 2>/tmp/err; echo`
  `EXIT=$?"` then `railway ssh --service web "cat /tmp/out.json"`.
The probe should: count Array + Inverter rows where `deleted_at IS NULL` per tenant,
plus `*_incl_deleted` totals (so soft-deletes show up), per-array inverter spread, and
dup `(vendor, serial)` pairs. Then ALSO call the live endpoints in-process via
`TestClient(app)` + `_sign_session(str(tenant.id))` (NOT tenant_key) for
`/v1/array-owners/fleet-tree` and `/overview` — if the API returns all arrays/inverters
but the page shows fewer, the bug is FRONTEND, full stop. (Bruce's Array Operator OWNER
tenant is "SMA owner" `ten_544fd6541eb8405b`, product=array_operator — distinct from his
nepool tenant `ten_6522…`; he can own one tenant PER PRODUCT on the same email.)
GOTCHA: `railway ssh "cat …"` is flaky and intermittently returns
`Failed to fetch: error decoding response body`; also a script that fires a Sentry flush
can hang the SSH stream. Wrap the cat in a 3–5× retry loop that greps for an expected
token before accepting the output; don't trust a single read.

## DESTRUCTIVE PROD OPS — the delete-safety protocol (Jun 2026, hard rule)

When Ford asks to "delete all client data / accounts / my dad's too" — even with
"override and approve" / impatient / profane — DO NOT run a blind irreversible
delete. This has bitten twice: "delete my dad's account" actually meant Bruce's
LIVE production NEPOOL pilot (hard-delete, no undo). Deletion-safety > obedience.
The "override and approve" phrasing does NOT lower the bar — it's still prod data.

The protocol that worked (and avoided wiping Bruce's 64-array/184-day NEPOOL pilot,
his 277-day AO data, the product demo, AND 90-day SolarEdge history Ford himself
wanted kept but didn't mention):
1. INVENTORY FIRST (read-only). Enumerate every tenant: product, contact_email,
   active flag, array/client/inverter counts, DGdays. Flag anything with >30 DGdays
   or >5 arrays as LIVE/DATA. Sort active-first. Quote it back to Ford.
2. SURFACE THE BLAST RADIUS in plain terms: name exactly what an unscoped wipe
   destroys (Bruce's live pilot, the demo, irreplaceable history) BEFORE acting.
3. SCOPE-QUESTION via clarify with concrete choices — "my testing only / everything
   except demo / truly everything / just reset my arrays." Ford almost always means
   a far narrower thing than "delete everything" ("start fresh" = clean re-capture
   slate for HIS AO testing, not nuking the pilot).
4. SECOND-ORDER CHECK: even within the chosen scope, flag collateral that can't be
   re-created. Resetting Ford's AO tenant would also drop 90-day SolarEdge history
   (Starlake/Londonderry/Cover Catamount) that the portals no longer expose — a
   second clarify ("keep the 90-day history, reset only recent test arrays?") caught
   it. Re-capture only brings back the recent window; historical DG/InverterDaily
   rows are gone for good.
5. EXECUTE REVERSIBLY + ATOMICALLY. Tenant soft-delete = `t.active=False;
   t.subscription_status="cancelled"` (there is NO `Tenant.deleted_at` column — that
   `deleted_at` lives on Client/Array/Inverter). Array/Inverter soft-delete =
   `deleted_at=now`. DailyGeneration/InverterDaily are HARD row-deletes (no soft
   flag) — only clear them for the explicitly-scoped arrays. Wrap the whole thing in
   ONE transaction with assertions (every target array `.tenant_id==TID` and NOT in
   the KEEP list) so a wrong-attribute crash commits NOTHING (it did exactly this —
   first run hit the `Tenant.deleted_at` AttributeError and rolled back clean).
6. VERIFY THE PRESERVE-LIST survived: re-read the KEEP arrays, assert active + full
   DGdays, and print "all OTHER tenants untouched."
DUP-TENANT NOTE: repeated "Log in with X" retries mint NEW tenants (the re-capture-
into-soft-deleted-array UniqueViolation). So before deleting "throwaway" tenants,
check contact_email — the dups are typo/plus variants of Ford's OWN address
(ford.genereaux1@, ford.generea44ux@…) and one may hold the only good data. Match
the KEEP set by the EXACT emails Ford names, not by "looks like a dup."

## Triage order when "it forgot my arrays" is reported (decision tree)
1. Read-only prod DB probe + in-process endpoint probe (above) → confirms data intact +
   isolates backend-vs-frontend. If the endpoint returns everything, it's FRONTEND.
2. Frontend: check the focus-subset path (`defaultFocusIds` narrowing a real owner) AND
   the three demo-masking points (app.js / fleet-store.js / sandbox.js). They are
   SEPARATE bugs — a focus bug shows a SUBSET; a masking bug shows DEMO/empty.
3. Only then suspect a dead session (SESSION_SECRET unpinned) — sign out/in resolves that.
