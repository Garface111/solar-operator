# EnergyAgent — Array Owners API Contract (v1)

Shared contract between the backend (api/array_owners.py) and the dashboard
(web/app ArrayOverview screen). Both sides build against THIS document.

## GET /v1/array-owners/overview
Auth: standard tenant bearer (tenant_from_bearer).

Response:
{
  "generated_at": "2026-06-12T21:30:00Z",
  "arrays": [
    {
      "array_id": 14,
      "name": "Starlake",
      "client_name": "Green Mountain Community Solar",
      "fuel_type": "solar",
      "live": {                       // null when no live source connected
        "source": "solaredge",        // solaredge | none
        "current_power_w": 4830.5,    // instantaneous W from inverter
        "as_of": "2026-06-12T21:29:12Z"
      },
      "today": { "kwh": 31.2 },       // null when no daily data for today
      "month": { "kwh": 612.4 },
      "lifetime": { "kwh": 48211.0 }, // sum of DailyGeneration rows
      "value": {
        "today_usd": 6.55,
        "month_usd": 128.60,
        "lifetime_usd": 10124.31,
        "breakdown": {
          "energy_rate_usd_per_kwh": 0.21,   // retail offset rate used
          "rec_usd_per_mwh": 35.0,           // REC market price used
          "energy_usd": ...,                 // generation × rate
          "rec_usd": ...                     // floor(MWh) × rec price
        }
      },
      "health": {
        "status": "ok",               // ok | stale | offline | no_source
        "last_data_day": "2026-06-11",
        "days_since_data": 1,
        "message": "Reporting normally"
      }
    }
  ],
  "totals": {
    "current_power_w": ...,           // sum of live arrays
    "today_kwh": ..., "month_kwh": ..., "lifetime_kwh": ...,
    "today_usd": ..., "month_usd": ..., "lifetime_usd": ...
  }
}

## Health status rules
- no_source: array has neither solaredge_api_key nor any DailyGeneration rows
- offline:   live source configured but SolarEdge overview call failed
- stale:     last DailyGeneration row is > 3 days old
- ok:        otherwise

## Value model (VT initial)
- energy_rate_usd_per_kwh: from api/rates.py VT_RATES table, keyed by the
  array's utility provider when known, default 0.21 (VT blended residential).
- rec_usd_per_mwh: env REC_PRICE_USD_PER_MWH, default 35.0.
- value = kwh * rate + floor(kwh/1000) * rec_price (lifetime only for REC part;
  today/month REC value is pro-rated kwh/1000 * rec_price, no floor, labeled
  "estimated").

## POST /v1/array-owners/arrays/{array_id}/solaredge
Body: { "api_key": "...", "site_id": 12345 }
Validates the key with a live SolarEdge overview call before saving.
Returns: { "ok": true, "site_name": "...", "peak_power_kw": ... } or 400 with
{ "detail": "reason" }.

## SolarEdge live power
GET https://monitoringapi.solaredge.com/site/{id}/overview?api_key=...
-> response.overview.currentPower.power (W), .lastUpdateTime,
   .lifeTimeData.energy (Wh), .lastDayData.energy (Wh), .lastMonthData.energy (Wh)
Cache server-side for 5 minutes per site (300 req/day budget).
