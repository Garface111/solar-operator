# Enphase (Enlighten v4) — adapter status + the exact grounding ask (Jun 21 2026)

Added `api/inverters/enphase.py` to widen vendor support — Enphase is the dominant
US residential inverter brand, so it's the single biggest addressable-market lever
for AO onboarding. Shipped + live; status is **code-complete, UNVERIFIED against a
live account** (same posture as SMA), gated honestly.

## What shipped (live on prod, verified)
- Adapter `api/inverters/enphase.py`: uniform interface (`validate` / `fetch_live`
  / `fetch_daily` / `discover_sites` + metadata), OAuth2 token caching + refresh
  rotation, grounded to the published v4 contract.
- Registered in `VENDORS` (#2, after SolarEdge). Live `/v1/array-owners/inverter-vendors`
  returns it (label "Enphase (Enlighten)", available, 5 fields). ✓ checked on prod.
- AO onboarding catalog (`array-operator/public/onboarding.html`) shows the Enphase
  tile with honest "in final verification" copy. ✓ live on arrayoperator.com.
- 8 mocked-shape tests (`tests/test_inverters_enphase.py`) + the vendors-listing
  assertion updated. 42 inverter tests pass.

## Grounded contract (from developer-v4.enphase.com, 2026-06-21)
- OAuth: token at `https://api.enphaseenergy.com/oauth/token`, **Basic auth** =
  base64(`client_id:client_secret`). Grants: `authorization_code` (hosted OAuth),
  `refresh_token` (rotates on use), `password` (partner flow). Access token ~1 day,
  refresh ~1 month.
- Every API call: base `https://api.enphaseenergy.com/api/v4`, `?key=<api_key>`
  (the app key) **and** `Authorization: Bearer <access_token>`.
- `GET /systems` → account-level list (system_id, name, system_size Wac; energy_*
  may be -1). `GET /systems/{id}/summary` → `current_power` (W), `energy_today`/
  `energy_lifetime` (Wh), `last_report_at` (epoch s). `GET /systems/{id}/energy_lifetime`
  → `start_date` + `production[]` (Wh per day).
- The 2026-03-16 v4 deprecation retires only MANAGEMENT endpoints (ACB telemetry,
  meter/array/tariff/user ops) — the systems/summary/energy_lifetime endpoints used
  here are NOT affected.

## THE GAP (honest): no live Enphase account/app yet
Built to docs, not run against a real token — first live connection may surface a
field rename or auth quirk (exactly what happened nowhere-near-rarely with the
other 🟡 vendors). Do NOT tell a customer this is SolarEdge-solid.

## EXACT ask to Ford (this closes it)
1. **Register an Enphase app** at developer-v4.enphase.com (free "Watt" plan to
   start; confirm it exposes `/systems`, `/summary`, `/energy_lifetime`). That
   yields the **api_key + client_id + client_secret**.
2. **One real Enphase owner** to authorize (hosted OAuth) or supply Enlighten
   login (partner password grant) → run a live `validate`/`fetch_live`/`fetch_daily`
   and confirm the JSON shapes match the contract above. (Bruce, or any AO prospect
   on Enphase.)
3. **Decide the plan tier** — the free Watt plan is rate-limited; if we onboard
   many Enphase owners we'll need Kilowatt/partner. Surface cost before scaling.

## Follow-up (not blocking): hosted-OAuth onboarding
The real zero-key UX is Enphase **hosted OAuth** (owner clicks Connect → Enlighten
consent → we get tokens), which needs a backend OAuth-callback route + the AO
onboarding redirect handoff (not built — the current UI collects app fields, which
suits a technical early adopter). The adapter already supports the `refresh_token`
grant, so wiring the callback is the only remaining piece. Mirror SolarEdge's
discover cascade once grounded (`discover_sites` is already implemented:
api_key → all systems).
