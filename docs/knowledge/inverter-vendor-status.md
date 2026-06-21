# Multi-vendor inverter support — verified status & the real blocker

The Array Operator inverter framework (`api/inverters/`) is a UNIFORM plug-in interface:
each vendor module exposes `validate` / `fetch_live` / `fetch_daily` + a metadata block
(`LABEL`, `AVAILABLE`, `NOTE`, `SUPPORTS_LIVE/DAILY`, `FIELDS`), and registers in the
`VENDORS` dict in `api/inverters/__init__.py` (insertion order = connect-UI order). The
connect UI renders each vendor's fields from `vendor_catalog()`; the peer-analysis engine
consumes any vendor's data identically. So MATURING a vendor is a module change, not an
architecture change. 37 vendor tests pass (`tests/test_inverters.py`,
`tests/test_inverter_fleet.py`) — but tests use MOCKED HTTP responses, so green tests prove
the code shape, NOT that the live API behaves as documented.

## The honest tier list (verified from code + tests Jun 2026 — do NOT overstate to a customer)

| Vendor | Status | What's real | Blocker |
|--------|--------|-------------|---------|
| **SolarEdge** | 🟢 LIVE-PROVEN | `discover_sites` (1 key→all arrays, account-level), real `fetch_live`/`fetch_daily`/inventory/per-inverter telemetry | none — running on Bruce's hardware (Londonderry 6 inv, Cover) |
| **Locus Energy** | 🟡 code-complete, UNVERIFIED | OAuth + `validate`/`discover_sites`(partner-level)/`fetch_live` against SolarNOC v3 | client_id/secret are **account-manager-gated** (no self-serve) → can't E2E without a real Locus account |
| **Fronius** | 🟡 code-complete, can't run in US | built+unit-tested vs Solar.web Query API documented shapes | Query API is **paid + NOT offered in the USA**; US path is the local **LAN Solar API** the Railway backend can't reach (owner's browser can → that's the extension vector, a PLAN not built) |
| **SMA** | 🟡 code-complete, ENDPOINTS UNVERIFIED | OAuth + ennexOS calls written (`fetch_live` reads `pvGeneration`) | module's own NOTE says "Endpoints unverified"; needs developer-app registration + owner consent |
| **Chint/CPS** | 🟢 LIVE via EXTENSION capture | NOT a key API — extension reads monitor.chintpowersystems.com responses passively and POSTs to `/v1/array-owners/inverter-capture` (chint in `_CAPTURE_VENDORS`). `inverters/chint.py` key path stays a stub by design. Working on Bruce's fleet (v1.9.22+). | PER-SITE: extension only sees a site's inverters once the owner OPENS that site (the `busTypeDevices` response fires on open). Multi-site owners must click into EACH site once — opening a site captures ALL its inverters at once (not per-inverter). UI tip is wired on the Chint login button + portal-open note. |
| **AlsoEnergy (PowerTrack)** | 🟡 code-complete, UNVERIFIED | CLEAN documented REST API (api.alsoenergy.com) — backend credential pull like SolarEdge/Locus, NOT scraping. OAuth password grant (username+password, no client_id). `validate`/`fetch_live`/`fetch_daily`/`discover_sites`. See alsoenergy-api-contract.md. | field names (AC power / energy) + BinData column mapping are candidate-list guesses; needs ONE real AlsoEnergy login to confirm. Paul Bozuwa's Danville (site 59947) is the fixture but he lacked creds. |

## The pattern that actually matters
**The recurring blocker across Locus/Fronius/SMA is NOT code — it's getting REAL credentials
to verify against.** SolarEdge is live ONLY because we have Bruce's key. The other three are
written to their documented APIs but unproven because we've never held a live account for any
of them. So "do we support vendor X?" has THREE honest answers, not two:
  1. **live-proven** (SolarEdge) — say this confidently.
  2. **supported pending your credentials** (Locus/Fronius/SMA) — code exists, first real
     connection may surface a doc-vs-reality mismatch (auth quirk, field rename). Do NOT tell a
     customer this is as solid as SolarEdge.
  3. **manual/CSV only** (Chint) — no API, honest by design.

When asked "where do we stand on vendor support?": answer from the CODE (read each module's
metadata + whether fetch fns make real HTTP calls vs raise), confirm tests pass, and give the
tier list — never assert "we support SMA" with SolarEdge-level confidence.

## How to push a 🟡 vendor to 🟢
The unblock is almost always a credential, so the move is to GET one live account and run a
real E2E (the live API will throw something the mocks didn't). Likeliest reachable: Locus (ask
if Bruce / a contact has SolarNOC access). Fronius-in-US specifically needs the LAN/extension
path (plan: `docs/plans/2026-06-13-extension-inverter-capture.md`, needs a real device to
inspect — can't write scraper selectors blind). Tighten the connect-UI honesty so it visibly
distinguishes "live-proven" from "supported, pending your credentials" so signup never
overpromises.

## Keeping the data LIVE (not just connected) — see data-hub-live-telemetry.md
A vendor being "connected" is NOT the same as the data being current. API-pullable vendors
(SolarEdge) refresh on demand; extension-capture vendors (Chint, and SMA/Fronius until their
API creds are wired) only update when the owner re-captures, so between captures we were
showing a stale afternoon peak as "producing now" at night — the Tannery Brook bug. The
server-side **poller** (`api/poller.py`, 5-min scheduled, daylight-gated) + the
`InverterReading` time-series table + the `_live_power_w` daylight honesty gate are the fix
that makes the product a continuous data hub. Full architecture, the scaling trap (SolarEdge
per-inverter rate limit → batch at site level), and the SMA app-registration blocker are in
`references/data-hub-live-telemetry.md`.
