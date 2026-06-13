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
- [ ] Ford decision: which brand to spike first
- [ ] A real inverter-portal login OR saved portal HTML/JSON sample to inspect (REQUIRED)
- [ ] Confirm the Array Overview UI is the render target (peer bars exist, need live feed)
