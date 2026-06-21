# Adding a new inverter/monitoring vendor to Array Operator

How to add per-inverter PRODUCTION support for a new vendor an owner uses. This
is critical-path work (Ford: "not a complete product until we support ALL
vendors people have"). Two fundamentally different integration shapes exist —
pick the right one FIRST, because it determines the whole build.

## STEP 0 — utility vs inverter vendor (don't conflate)

When an owner lists their fleet they mix two kinds of "vendor":
- **Utility** (GMP, VEC, …) = the electric company. Measures consumption + net
  export at the METER. Already supported as a BILL source (api/adapters/gmp.py,
  api/adapters/vec.py→smarthub, rates.py). Gives whole-array net-metered kWh and
  $ value, NEVER per-inverter breakdown.
- **Inverter / monitoring vendor** (SolarEdge, Fronius, SMA, Chint, AlsoEnergy)
  = the production data source. ONLY these give per-inverter telemetry, which is
  what the peer-analysis / "which inverter underperforms" product is built on.

So "add GMP/VEC for data gathering" ≠ "add an inverter vendor." If asked for a
utility's PRODUCTION interval data, that's a real but SEPARATE feature (whole-
array proxy for owners with no inverter monitoring) — scope it, don't assume.

## STEP 1 — recon: which integration shape?

Two shapes, in order of preference:

### Shape A — KEY-BASED BACKEND PULL (preferred; SolarEdge, Locus, AlsoEnergy)
The vendor has a real (often documented) REST API you authenticate to with the
owner's credentials or an API key, and pull from the backend. NO browser
extension. Always check for this FIRST — it's far cleaner than scraping.
- Look for `api.<vendor>.com`, a `/swagger` or `/swagger/v1/swagger.json`
  OpenAPI spec, developer docs. AlsoEnergy: `https://api.alsoenergy.com/swagger/v1/swagger.json`
  (155KB) gave the entire contract — auth scheme, endpoints, response schemas —
  in one file. PULL AND PARSE THE OPENAPI SPEC; it's the equivalent of a HAR but
  authoritative.
- Probe auth live with a deliberately-bad credential to learn the mechanism
  WITHOUT real creds: `curl -s -X POST https://api.alsoenergy.com/Auth/token -H
  "Content-Type: application/x-www-form-urlencoded" --data-urlencode
  "grant_type=password" --data-urlencode "username=test@example.com"
  --data-urlencode "password=invalid"` → `{"error":"Wrong email or password."}`
  403 proved the password grant works WITHOUT client_id/secret. A "client_id
  required" error would have meant an app registration is needed.
- Confirm there's NO unauthenticated shortcut before relying on creds: a public
  "display" link (AlsoEnergy public-display GUID) usually only serves the SPA
  shell — the data API still 401s without a token. Test `GET /Sites/{id}` with
  no bearer; a 401 means the owner genuinely needs login credentials.

### Shape B — EXTENSION CAPTURE (only when no usable API; Fronius cloud, SMA, Chint)
No owner-facing key, encrypted/per-request-bound token, or paid/high-friction
API → read the data the owner's logged-in portal already loads. See the main
SKILL.md + the auth-pattern / passive-observation sections of this file's sibling
`extension-capture-debugging.md`. This is the fallback, not the default.

## STEP 2 — build the backend (Shape A): the two-file Locus pattern

Mirror `api/adapters/locus.py` + `api/inverters/locus.py` EXACTLY — it's the
canonical key-based template (read both before writing). AlsoEnergy
(api/adapters/alsoenergy.py + api/inverters/alsoenergy.py) is a second worked
example to copy.

1. **`api/adapters/<vendor>.py`** — the HTTP layer (single source of truth):
   - Exception family: `<V>Error` (base), `<V>AuthError` (401/403 bad creds),
     `<V>ScopeError` (valid creds, no access to the entity). Translate HTTP
     status → these in one `_request()` helper (401→Auth, 403→Scope, 429/5xx→Error).
   - Module-level token cache keyed by username/credential:
     `{key: (access_token, refresh_token, expires_at)}`. Reuse until ~60s before
     expiry; refresh-grant first, fall back to a fresh password grant.
   - ⚠️ OAUTH REFRESH-TOKEN ROTATION TRAP (root-caused SMA "worked until I
     reconnected", Jun'26). Some providers (SMA = `auth.smaapis.de`) ROTATE the
     refresh_token on EVERY refresh grant: the response returns a NEW
     refresh_token and INVALIDATES the one just sent. If the adapter discards the
     new token (the original SMA bug), the first refresh (~1h post-connect)
     works, the SECOND reuses the now-dead original → 401 → the plant goes dark
     SILENTLY until the owner manually reconnects (which only works because
     reconnect writes a fresh token). The tell that it's THIS bug and not a blip
     or bad creds: **reconnecting fixes it**. Required handling (AlsoEnergy does
     the in-memory half; SMA now does both):
       1. Capture the rotated token: `new_refresh = body.get("refresh_token") or
          old_refresh`; cache the freshest one AND prefer it over config's.
       2. PERSIST it to the DB. The adapter writes `new_refresh` back into the
          `config` dict IN PLACE; the caller must save it. JSON columns DON'T
          auto-detect nested mutation → the poller's `_persist_config_if_changed`
          re-assigns `conn.config` + `flag_modified(conn,"config")` then commits.
          Without persistence the token survives only the process lifetime — a
          Railway redeploy resets to the original (already-consumed) token and it
          dies again. This is the part most adapters miss.
       3. On a 401 from the refresh grant, CLEAR the dead token from config
          (`config["refresh_token"]=None`) + drop the cache entry so the next
          call falls back to a client_credentials/password grant instead of
          retrying a known-bad token forever.
     Regression test pattern: tests/test_sma_token_rotation.py — mock httpx.post
     to issue access-N/refresh-N each call, force cache expiry, assert the 2nd
     refresh SENDS refresh-1 (not the dead refresh-0) and config got refresh-2;
     a 401 path clears config + cache.
   - Resilient field discovery: when register/field names (AC power, energy)
     aren't pinned by the spec, keep a PRIORITIZED candidate list and try each,
     using the first that returns data; log what's found. (AlsoEnergy
     `_AC_POWER_FIELDS` / `_ENERGY_FIELDS`.) Hard timeout on every call (30s).
2. **`api/inverters/<vendor>.py`** — the thin vendor-interface wrapper:
   - `CODE`, `LABEL`, `AVAILABLE=True`, `SUPPORTS_LIVE/DAILY`, `NOTE`, `FIELDS`
     (list of {name,label,secret}). `_creds()`/`_site_id()` validators.
   - `validate` / `fetch_live` / `fetch_daily` / `discover_sites`, each
     translating the adapter exception family → InverterAuthError/ScopeError/Error
     (from `.base`).
3. **Register** in `api/inverters/__init__.py`: add to the `from . import …` line
   AND the `VENDORS` dict. `vendor_catalog()` then auto-exposes it at
   `/v1/array-owners/inverter-vendors`, and the generic `/connect-single` +
   `/arrays/{id}/inverter` paths dispatch to it — NO new backend endpoints needed
   for a standard credential vendor.
4. **Tests**: `tests/test_inverters.py::test_inverter_vendors_listing`
   HARD-PINS the exact vendor set + per-vendor field counts (registry-enumeration
   assertion). Adding a vendor WILL fail it until you update that assertion —
   that's expected, update it. Run `python -m pytest tests/test_inverters.py
   tests/test_array_owners.py -q` (use `.venv`, not the stale `venv` in CLAUDE.md).

## STEP 3 — wire the FRONTEND (the gotcha that bites)

The live Array Operator picker does NOT auto-render from the backend
`vendor_catalog`. The `VENDORS` array is HARD-CODED in BOTH:
- `/root/array-operator/public/sandbox.js` (the Add-array modal)
- `/root/array-operator/public/onboarding.html` (the signup wizard)
A vendor missing from these two won't appear no matter what the backend says.
Add the same `{code,label,meta,available,discover,note,fields:[…]}` entry to
BOTH, and add the code→label to the `BRAND` maps in sandbox.js, layout-view.js
(and fleet-store.js if present) so it labels on the canvas. Key-based vendors use
`discover:false` and route through `/v1/array-owners/connect-single`.

## STEP 4 — honesty about live confirmation

A spec-grounded adapter is NOT a confirmed-working one. Field names + response
shapes from an OpenAPI spec are still assumptions until exercised against a real
account. Say "built to the documented contract, needs one live login to confirm
field names" — don't claim "working." Real owners' fleets are the test fixtures
(Ford greenlights using their live creds; store chmod600, warn to rotate).

## Deploy reminder (both repos)

- Backend (solar-operator): commit + push; Railway deploy mechanism — confirm
  it's git-auto-deploy before claiming the backend is live (don't assume).
- Frontend (array-operator): commit + push + `netlify deploy --prod --dir=public
  --site=966cb1f5-944e-41fd-855b-10053edc5d18` (NOT git-auto-deploy — repo_url:null).
  Verify live with `curl https://arrayoperator.com/sandbox.js | grep <vendor>`.
