# Plan: Extension-based Inverter Capture for Array Operator

**Date:** 2026-06-13
**Status:** PROPOSAL — awaiting Ford's go before any adapter is built
**Vector:** "EnergyAgent meets your hardware wherever it lives." One browser agent serves
BOTH products — utility bills (NEPOOL Operator) and inverter truth (Array Operator).

## The idea (Ford, Jun 2026)

> "For the array operator part of the service, can the extension be used to add their
> inverter data from the inverter sites? Could be a great upgrade that boosts the
> sublimeness of everything."

Yes. The extension is a logged-in-session scraper: the user is already authenticated in a
portal; the content script reads the page (or its JSON API) using existing cookies and ships
the result to the backend. No passwords, no API keys stored. That same pattern maps onto
inverter monitoring portals (SolarEdge, Enphase, Fronius SolarWeb, SMA, Chint).

## The strategic fork (this is the whole point)

The extension is NOT a blanket replacement for the official inverter API framework
(`api/inverters/`). Where a real API exists AND the owner can hand us a key, the API wins:
structured, stable, historical data in one call, doesn't break on a dashboard restyle. The
"one credential, all arrays" SolarEdge flow already built for Bruce is the gold path.

**The extension earns its place exactly where the API path can't reach** — and those cases
are already documented in the project:

| Brand | API reality (from project memory) | Extension value |
|-------|-----------------------------------|-----------------|
| **SolarEdge** | Official API, account-level key lists all sites | LOW — API is better. Extension only as a no-key fallback. |
| **Locus (SolarNOC)** | Built-in vendor, v3 API, OAuth | LOW — API path exists. |
| **Fronius** | Cloud API is **paid + not offered in USA**; only free path is **local LAN Solar API** which Railway CANNOT reach | **HIGH** — owner's browser is ON the local network; server is not. Capability the backend literally cannot have. |
| **Chint / CPS** | **No public API at all** (honest stub today) | **HIGH** — scraping the web portal is the only automated route that exists. |
| **SMA** | Needs OAuth app registration + per-owner consent (high friction) | **MEDIUM** — extension is the zero-setup fallback. |
| **Enphase** | v4 API exists (per-micro = per-panel truth) but per-owner OAuth | MEDIUM — API better if owner consents; extension lowers friction. |
| **Any owner who won't generate an API key** | — | **HIGH** — "install + log in like you already do" beats "find your API key in settings" for non-technical owners. |

**Framing for the owner (kitchen-table, dollar-led):**
> "Got a SolarEdge account? One credential, all your arrays, done. Got a Fronius on your home
> network or a Chint with no API? The same little browser agent that files your NEPOOL reports
> reads your inverter too — no keys, no setup, just sign in like you already do."

This is the umbrella synergy in one sentence and the answer to "will it work with
everything?" — **yes**, because between official APIs and session-scraping there's no inverter
we can't reach.

## Architecture (clone the proven SmartHub pattern)

The SmartHub adapter is the template. Per inverter brand:

1. **Content script** keyed to the portal host (e.g. `*.solaredge.com`, `*.solarweb.com`,
   local Fronius IP range for LAN). Reads the dashboard's own JSON API first (zero-click,
   richest — same 3-layer strategy as `smarthub_content.js`), DOM fallback second.
2. **Normalize to a "unit"** — the peer-analysis engine is already unit-agnostic
   (`api/inverters/peer_analysis.py analyze_cohort(units)`; a unit = `{nameplate_kw, daily kWh,
   error_code, last_report}`). Inverter captures drop straight in with ZERO engine rework.
   This is the key leverage: the brain already exists and is shaped for exactly this.
3. **New backend endpoint** `POST /v1/array-owners/inverter-capture` (mirror `/v1/sync`),
   accepting BOTH session token and tenant key (the dual-auth rule — see project skill).
4. **Fleet learning** — unknown inverter hosts mint `inv_<subdomain>` provider codes, same
   self-improving discovery loop as `sh_<subdomain>` for SmartHub.

### Reuse, don't rebuild
- Bridge (`so_bridge.js`), SO_* protocol, badge/notification plumbing — all reused as-is.
- The manifest just gains the inverter hosts in `host_permissions` + a new content_scripts
  entry. (Note: each new host triggers a Chrome Web Store re-review — batch them.)
- `peer_analysis.py` cohort logic, statuses, the Array Overview UI — all already built and
  waiting for live data.

## Caveats — flag LOUDLY (per Ford's trust-check style)

1. **CANNOT build scrapers blind.** I have no inverter-portal credentials and no live
   dashboards to inspect. Writing selectors/JSON-paths without a real portal = fabricated
   guesses that break on contact (violates the "never fabricate an integration" rule). Each
   adapter needs EITHER a real login to inspect OR a saved HTML/JSON sample of the portal.
   **This is the gating dependency for any build.**
2. **Fronius-local is the highest-value but trickiest** — the LAN Solar API lives at the
   inverter's local IP (e.g. `http://192.168.x.x/solar_api/v1/...`). A content script can
   `fetch()` it from the owner's network, but: mixed-content (https page → http LAN) and
   the owner needs to know/find the inverter IP. Worth a dedicated spike.
3. **Each brand portal restyle can break a scraper** — same maintenance tax as SmartHub.
   Acceptable for the no-API brands; not worth it where an API exists.
4. **Web Store re-review per host batch** — adding inverter hosts = another multi-day review.
   Plan the host list once, add them together.
5. **Per-panel vs per-inverter granularity varies** — Enphase gives per-micro (per-panel)
   truth; string inverters give per-inverter. The peer engine handles any granularity, but
   the owner-facing copy should be honest about resolution per brand.

## Recommended sequencing (when Ford greenlights)

1. **Spike Fronius-local** (highest unique value — backend literally can't do it) OR
   **SolarEdge via extension** (easiest — well-documented portal, lets us prove the pattern
   end-to-end against something we can actually test).
2. Wire the spike's capture → `analyze_cohort` → Array Overview UI (the peer-index bars +
   diagnosis that are built but not yet fed live data — see project skill, "remaining half").
3. Expand to Chint + SMA once the pattern + the capture→engine→UI loop is proven.

## Dependencies to unblock a build
- [x] Ford decision: which brand to spike first → **SolarEdge** (2026-06-14)
- [x] A real inverter-portal login OR saved portal HTML/JSON sample to inspect → **inspected Bruce's live account 2026-06-14** (grounded contract below)
- [ ] Confirm the Array Overview UI is the render target (peer bars exist, need live feed)

---

## SolarEdge extraction — GROUNDED CONTRACT (inspected Bruce's live account, 2026-06-14)

Inspected `monitoring.solaredge.com` (the new "one" SPA) while logged in as Bruce
(account "Green Mountain Community Solar", 3 sites). Every endpoint below is
**session-cookie authed** (the content script calls them with `credentials:'include'`),
returns JSON, and was verified live. No guessing remains.

**The capture chain (what `solaredge_content.js` runs on `monitoring.solaredge.com`):**

1. **Identity** — `GET /services/cni/ui-api/user-info`
   → `{ accountId, email, firstname, lastname, userId, monitoringId }`
2. **Account GUID** — `GET /services/account-admin/accounts?page=1&size=20`
   → `{ pagination, items:[ { accountUuid, accountName, … } ] }`  (`accountUuid` = the GUID)
3. **Durable API key** — `GET /services/account-admin/accounts/{accountUuid}/api-key`
   → the account's **already-generated** public API key.
   **READ-ONLY GET. NEVER POST/PUT/regenerate** — a new key invalidates the old one and
   would break the owner's existing integrations (possibly our own backend's stored key).
   If 404/empty, the account has no key yet → fall back to asking the owner (or a
   guarded generate-with-consent); do NOT silently mint one.
4. **Instant site list (zero-key preview)** — `POST /services/sitelist/searchSites?v=<ts>`
   body `{}` (empty works) → `{ totalSitesInSearch, page:[ {
     solarFieldId,   // <-- the SolarEdge SITE ID the public API + our backend use
     name, peakPower (kW), status, inverterCount, optimizerCount, accountId,
     accountName, city, state, installationDate, latitude, longitude, … } ] }`

**Flow (clones the GMP/SmartHub pattern + reuses the EXISTING array-operator backend):**
- AO onboarding (extension present + SolarEdge chosen) → `SO_OPEN_PORTAL { url: monitoring.solaredge.com }`
  (background opens a bg tab; cookie-wipe list must add `solaredge.com`).
- `solaredge_content.js` detects logged-in (user-info 200), runs steps 1–4, posts a capture
  `{ provider:"solaredge", apiKey, sites:[…], user:{…} }` to background.
- background broadcasts `SO_CAPTURE_LANDED { provider:"solaredge", apiKey, sites }`
  (extend `broadcastToSoTabs` SO_TAB_URLS + manifest to include arrayoperator.com).
- AO page consumes it: drops `apiKey` into `state.apiKey` and runs its EXISTING code paths —
  `/v1/array-owners/public/preview` (the value reveal), then post-signup
  `/v1/array-owners/solaredge/connect-account`. **Zero backend changes.**

**Manifest changes (batch into ONE Web Store review):**
- host_permissions + a `content_scripts` entry for `https://monitoring.solaredge.com/*` → `solaredge_content.js`.
- Add `https://arrayoperator.com/*` + `https://*.arrayoperator.com/*` to host_permissions,
  the `so_bridge.js` content_scripts match, and background `SO_TAB_URLS` (today the bridge
  can't reach AO at all).
- Add `solaredge.com` to the cookie-wipe allow-lists in `OPEN_UTILITY_PORTAL` / `SO_WIPE_COOKIES`.

**Login-state:** user-info 200 = signed_in; 401 / redirect to `/mfe/auth/` = login_required.

**Safety:** the api-key value is a live secret — never log/persist it beyond the in-memory
hand-off; the durable-key endpoint is GET-only by our rule (no regeneration).
