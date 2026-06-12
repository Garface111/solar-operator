# Multi-Vendor Inverter Framework (v1)

Generalizes the SolarEdge-only integration into a pluggable inverter-source
framework. Vendors: solaredge (existing, keep working), fronius, sma, chint.

## Data model
New table InverterConnection (api/models.py):
  id, array_id FK->arrays unique, vendor str(20), config JSON,
  status str(20) default "unverified",   # unverified | ok | error
  last_error Text nullable, last_sync_at DateTime nullable,
  created_at DateTime default now
Keep Array.solaredge_api_key/site_id for backward compat: on first read, if an
array has those fields and no InverterConnection row, treat it as a virtual
connection {vendor: "solaredge", config: {api_key, site_id}}.

## Adapter interface (api/inverters/__init__.py)
Each vendor module exposes:
  validate(config: dict) -> dict          # raises InverterAuthError/InverterError; returns {"site_name": str, ...}
  fetch_live(config: dict) -> dict | None # {"current_power_w": float, "as_of": iso} or None if unsupported
  fetch_daily(config: dict, start: date, end: date) -> list[{"day": date, "kwh": float}]
VENDORS registry dict: {"solaredge": module, "fronius": module, "sma": module, "chint": module}
Common exceptions in api/inverters/base.py: InverterError, InverterAuthError.
Existing api/adapters/solaredge.py logic is WRAPPED by api/inverters/solaredge.py (do not duplicate; import and adapt).

## Vendor specifics

### fronius — Solar.web Query API
Base: https://api.solarweb.com/swqapi
Auth headers on every request: AccessKeyId + AccessKeyValue. Config:
{access_key_id, access_key_value, pv_system_id}
- validate: GET /pvsystems/{pvSystemId} -> name, peakPower
- fetch_live: GET /pvsystems/{pvSystemId}/flowdata -> channels[] find channelName=="PowerPV" -> value (W); timestamp field "logDateTime"
- fetch_daily: GET /pvsystems/{pvSystemId}/aggrdata?from={YYYY-MM-DD}&to={YYYY-MM-DD} -> data[] each {logDateTime: "YYYY-MM-DD", channels: [{channelName: "EnergyProductionTotal", value: Wh}]} -> kwh = value/1000
LOUD CAVEAT in module docstring: Solar.web Query API is a CHARGEABLE business
API and per Fronius's country list is NOT currently offered in the USA.
Adapter is built and tested against the documented shapes; US customers may
need the local Solar API (LAN) path later.

### sma — Monitoring API (ennexOS / smaapis.de)
OAuth2. Config: {client_id, client_secret, system_id, refresh_token?}
Token endpoint: https://auth.smaapis.de/oauth2/token
- _get_token: client_credentials grant if no refresh_token, else refresh_token grant. Cache token in config-scoped module cache w/ expiry.
- validate: GET https://monitoring.smaapis.de/v1/plants/{system_id} (Bearer) -> name
- fetch_live: GET https://monitoring.smaapis.de/v1/plants/{system_id}/measurements/sets/EnergyAndPowerPv/Recent -> find set with pvGeneration power value (W)
- fetch_daily: GET .../measurements/sets/EnergyAndPowerPv/Day?Date={YYYY-MM-DD} per day in range (cap range at 90 days/call loop) -> pvGeneration energy Wh -> /1000
LOUD CAVEAT in docstring: requires app registration with SMA (client_id/secret
issued by SMA developer portal) AND owner consent flow; endpoints follow SMA's
published docs but are UNVERIFIED against a live account.

### chint — CPS/Chint cloud
NO public API documentation exists (June 2026 recon). Implement as an explicit
stub that registers in VENDORS but: validate() raises InverterError with
message "Chint/CPS cloud has no public API — connect via manual CSV upload for
now; we're tracking their FlexOM gateway for direct support." fetch_live
returns None, fetch_daily returns []. UI shows the vendor with a "manual data"
badge so the operator path is honest. This keeps the funnel (operator selects
Chint, gets clear guidance) without fabricating an integration.

## Endpoint changes (api/array_owners.py)
- POST /v1/array-owners/arrays/{array_id}/inverter  body {vendor, config}
  -> dispatch validate(); on success upsert InverterConnection(status="ok"),
  return {"ok": true, "site_name": ...}. Auth errors -> 400 {"detail": msg}.
  Keep the old /solaredge endpoint as a thin shim that forwards to this.
- GET /v1/array-owners/inverter-vendors -> [{code, label, fields:[{name,label,secret:bool}], available:bool, note}]
  (frontend renders the connect form from this — fronius: 3 fields, sma: 3-4, solaredge: 2, chint: available=false + note)
- overview: live power + daily backfill resolves through the connection's
  vendor module. Scheduler hook: extend the existing daily SolarEdge poll job
  to iterate ALL InverterConnection rows (see api/scheduler.py for the
  existing solaredge poll; generalize it).

## Frontend (web/app)
ArrayOverview Connect modal becomes vendor-aware: first a vendor picker
(cards with vendor name + availability note from /inverter-vendors), then a
form generated from `fields`. Chint card shows disabled state + the manual-CSV
note. Keep all visual language identical to the current modal.

## Tests
tests/test_inverters.py — per vendor: validate success + auth-failure (mock
httpx; no real network), fetch_daily parsing from canned JSON fixtures,
chint stub behavior, the new /inverter endpoint (session-token auth — copy
test_array_owners.py pattern), vendors listing endpoint, legacy /solaredge
shim still works, virtual-connection fallback from Array.solaredge_* fields.
