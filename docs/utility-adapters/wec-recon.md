# Washington Electric Co-op (WEC) — Adapter Recon

**Date:** Jun 3, 2026  
**Status:** Research only — no code written

## Login URL
https://washingtonelectric.smarthub.coop/ui/  
Login: https://washingtonelectric.smarthub.coop/Login.html

Credentials: Account number from bill. OpenIdLogin authentication flow.

## Portal Technology
**NISC SmartHub** — same platform as Vermont Electric Cooperative and Stowe Electric.
This is significant: a single adapter could potentially target all three NISC utilities.

## JSON API?
**Likely yes, behind login.** The shared NISC SmartHub API is documented at
https://apidoc-en.smh.smarthing.com/ — JSON/REST with ISO 8601 timestamps,
15-minute metering resolution available via API (hourly resolution via CSV
export since Jan 2024).

A community reverse-engineering project confirms this works for NISC SmartHub
utilities: github.com/tedpearson/electric-usage-downloader — returns
15-minute resolution data after SmartHub login. This is strong evidence that
the same approach would work against washingtonelectric.smarthub.coop.

**Could not verify generation-specifically without credentials.**

## Community Solar kWh Exposure?
**Uncertain.** WEC operates the ACRE program (Affordable Community Renewable
Energy) in partnership with VEC — $45/month bill credits for income-qualified
members, 115 slots enrolled Sep 2024. 

The SmartHub API returns metering data for all accounts, but whether it separates
generation vs. consumption for community solar accounts is unconfirmed. The
credit might show up as a billing line item rather than as raw generation kWh.

Contact: solar@weci.org for ACRE program technical questions.

## Estimated Build Effort
**Medium**

- Same NISC SmartHub platform as VEC/Stowe — adapter code reusable
- Public API documentation exists (unlike BED/SpryPoint)
- Authentication complexity: OpenIdLogin requires reverse-engineering the login
  flow (same as any NISC utility)
- Main risk: whether generation kWh is exposed per community-solar account
- ~4-8 weeks if generation data confirmed accessible; more if not

## Risk
**Medium** — NISC platform is well-understood and documented. Main risk is data
availability for community solar generation kWh specifically. If ACRE credits are
only billing-layer items (dollar credits, not kWh), the utility is not useful for
NEPOOL-GIS reporting.

## Recommendation
**Second priority after VEC.** Build one NISC adapter that handles
vermontelectric.smarthub.coop / washingtonelectric.smarthub.coop /
stoweelectric.smarthub.coop — the subdomain is the only difference for the login
and API base URL.

## References
- WEC ACRE program: https://www.washingtonelectric.coop/affordable-community-renewable-energy-program-acre/
- WEC solar: https://www.washingtonelectric.coop/tag/solar-generation/
- NISC API docs: https://apidoc-en.smh.smarthing.com/
- Reverse-engineering reference: https://github.com/tedpearson/electric-usage-downloader
