# Stowe Electric Department (SED) — Adapter Recon

**Date:** Jun 3, 2026  
**Status:** Research only — no code written

## Login URL
https://stoweelectric.smarthub.coop/ui/  
Login: https://stoweelectric.smarthub.coop/Login.html

Note: New SmartHub account numbers were issued Oct 2, 2023 when SED migrated
to NISC SmartHub. Customers who haven't yet claimed their new SmartHub account
may need to re-register.

## Portal Technology
**NISC SmartHub** — same platform as Vermont Electric Cooperative and Washington
Electric Co-op. SED's implementation is newer (Oct 2023) than VEC/WEC, which
suggests a more recent SmartHub version with potentially better API coverage.

## JSON API?
**Likely yes, behind login.** Same NISC SmartHub API surface applies:
https://apidoc-en.smh.smarthing.com/ — JSON/REST, 15-minute metering data,
same as WEC. The same reverse-engineering project would work here.

SED has received $6M in federal USDA PACE funding for grid modernization projects,
suggesting robust backend infrastructure and metering investment.

**Could not verify generation-specifically without credentials.**

## Community Solar kWh Exposure?
**Uncertain — program newly launched.** SED is bringing online a two-component
community solar project in 2025:
- Smith's Falls micro-hydroelectric + dam restoration  
- Moscow Mills solar array

These will provide on-bill credits for low-to-moderate-income households. The
SmartHub API likely exposes metering data for these accounts, but whether
generation kWh per community-solar account is separated from consumption billing
is unknown — the program is brand-new.

SED already operates 145 solar generation sites (3,000 kW) with the Nebraska
Valley 1MW array producing 1.57M kWh/year, so the metering infrastructure for
generation data exists.

## Estimated Build Effort
**Medium**

- Same NISC SmartHub platform — adapter code shareable with VEC/WEC
- Newer implementation (Oct 2023) may have cleaner API behavior
- Community solar program just launched; billing behavior not yet documented
- Would benefit from contacting SED to understand the billing model before investing
- SED contact: 802-253-7215, 435 Moscow Road

## Risk
**Medium** — NISC platform de-risks the technical side. Main uncertainty is
whether the new (2025) community solar program exposes generation kWh in the
API or only dollar bill credits. The program's newness means less third-party
documentation exists.

## Recommendation
**Bundle with WEC** — both are NISC SmartHub. A single NISC adapter parameterized
by subdomain (vermontelectric / washingtonelectric / stoweelectric) would cover
all three for the cost of one build. Confirm data model with VEC first (largest
program), then light-test against WEC and SED subdomains.

## References
- SED SmartHub announcement: https://www.stoweelectric.com/post/announcing-new-smarthub-billing-system
- SED solar generation: 145 sites, 3,000 kW (as of March 2024)
- USDA PACE funding: American Public Power Association article
- NISC API docs: https://apidoc-en.smh.smarthing.com/
- Reverse-engineering reference: https://github.com/tedpearson/electric-usage-downloader
