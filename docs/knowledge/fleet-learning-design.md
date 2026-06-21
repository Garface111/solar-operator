# Fleet-learning: discovered SmartHub utilities (shipped v1.6.2, Jun 12 2026)

Design rationale + runbook for the discovered-utility loop. Companion to the
"Fleet-learning loop" section in SKILL.md.

## Why hostname-authoritative

The original extension registry fell back to `provider: "vec"` for any unknown
`*.smarthub.coop` host ("backward compat"). Consequences:
1. Captures from new co-ops were ATTRIBUTED to VEC — wrong utility in the DB.
2. Because `/v1/sync`'s bill-persistence acct_map is provider-keyed, bills from
   those accounts silently dropped (sync returns 200; no bills appear).
This is the exact bug class that bit WEC. Closing it required making the page
hostname the source of truth on the backend too, since old extension versions
in the field will keep claiming "vec" forever.

## Code touchpoints

- `api/adapters/smarthub.py`
  - `DISCOVERED_PREFIX = "sh_"`
  - `derive_provider_from_host(hostname)` → `{provider, name, host, discovered}` | None
    - curated host → CSV-registry code, discovered=False
    - unknown smarthub.coop host → `sh_<sanitized subdomain>` (regex `[^a-z0-9]+`→`_`,
      ≤37 chars + prefix fits VARCHAR(40)), discovered=True
    - non-smarthub host → None
  - `parse_extension_payload`: hostname (payload `user.hostname`) wins over claimed
    provider; VEC fallback ONLY when no hostname (pre-1.6 payloads). Returns extra
    keys: `smarthub_host`, `smarthub_discovered`, `smarthub_display_name`,
    `capture_method`, `extension_version`.
  - `is_smarthub_provider` accepts `sh_*`.
- `api/adapters/__init__.py` `get_adapter`: `sh_*` → smarthub module.
- `api/app.py` `/v1/sync`: upserts DiscoveredUtility when discovered, increments
  capture_count, stamps method/version, one-time alert (alerted_at gate), adds
  `utility_discovered` capture event.
- `api/app.py` `/v1/extension/scrape-miss` POST: records `last_capture_method="miss"`
  + one-time alert. Curated hosts get a DiscoveredUtility row here too (rows double
  as drift telemetry).
- `api/models.py` `DiscoveredUtility`: host (unique), provider_code, display_name,
  first/last_seen_at, capture_count, last_capture_method, last_extension_version,
  promoted_code, alerted_at.
- `extension/smarthub_content.js`: `sendCapture(bills, usage, method)` adds
  `captureMethod` + `extensionVersion` to payload; `reportEmptyScrape()` fires
  `SMARTHUB_SCRAPE_EMPTY` once per page when all layers strike out after MAX_POLLS.
- `extension/background.js`: forwards SMARTHUB_SCRAPE_EMPTY to `/v1/extension/scrape-miss`
  (bearer = tenant key; base derived from sync endpoint).
- Registry template lives in `scripts/gen_smarthub_registry_js.py` — the JS fallback
  mints the SAME `sh_` code as the backend (keep the two sanitizers identical).
- Tests: `tests/test_discovered_utilities.py` (14 tests: derivation, masquerade
  removal, routing, sync e2e mint+increment, scrape-miss).

## Promotion runbook (when the alert email arrives)

```
# repo side (dev machine):
python -m scripts.promote_discovered_utility <host> \
    --code <curated_code> --label "Real Utility Name" --state VT
# → CSV row + registry regen + local-DB backfill; commit + push (deploys)

# prod side (after deploy):
railway ssh "cd /app && python -m scripts.promote_discovered_utility <host> \
    --code <curated_code> --db-only"
# → backfills prod sh_* rows, marks promoted
```

## Alert plumbing
`send_internal_alert(subject, body)` in api/notify.py → Resend →
INTERNAL_ALERT_TO (defaults to Ford's dysonswarmtechnologies.com address).
Returns False when RESEND_API_KEY unset (tests) — alerted_at only stamps on success,
so alerts retry next capture until one sends.

## Residual risks (stated to Ford, accepted)
- Guarantees failures are LOUD + recoverable, not that every deployment parses
  first-try. NISC version skew / third DOM layouts will surface via scrape-miss.
- Generation kWh availability varies per co-op (some expose only $ credits) — no
  extension fix possible; flagged in provider CSV notes.
