# Vermont Electric Cooperative (VEC) — Adapter Recon

**Date:** Jun 3, 2026  
**Status:** Research only — no code written

## Login URL
https://vermontelectric.smarthub.coop/Login.html  
(Alternate: https://vermontelectric.smarthub.coop/ui/)

Credentials: VEC account number + email. Password set via temporary-link email.

## Portal Technology
**NISC SmartHub** (National Information Solutions Cooperative). Proprietary
SaaS platform used by hundreds of rural electric cooperatives. AJAX/JSON
backend behind the SmartHub UI.

## JSON API?
**Could not verify without credentials.** Generic NISC SmartHub API docs exist
at https://apidoc-en.smh.smarthing.com/ — these describe a REST/JSON surface with
15-minute metering resolution. However, VEC has not published integration docs or
a developer portal. Any API access likely requires direct negotiation with VEC or
NISC, not self-service.

A community reverse-engineering project (github.com/tedpearson/electric-usage-downloader)
confirms NISC SmartHub exposes metering data via API after login; the same code
likely works against vermontelectric.smarthub.coop but this hasn't been tested.

## Community Solar kWh Exposure?
**Uncertain.** VEC's Co-op Community Solar program gives members a fixed monthly
bill credit — the portal shows consumption data and billing, but it is NOT
confirmed whether individual per-account generation kWh is exposed separately
from the credit.  

VEC is transitioning to Advanced Metering Infrastructure (AMI/RF-based smart
meters), which should improve data granularity, but no documentation confirms
the community solar kWh generation is surfaced for members.

**Could not verify without credentials.**

## Estimated Build Effort
**Medium-to-Hard**

- NISC SmartHub is documented and well-understood (same platform as WEC/Stowe)
- But generation data availability is unconfirmed — may only expose bill credits
- Would require credential testing + direct contact with VEC/NISC before investing
- Contact: 1-800-832-2667 or info@vermontelectric.coop

## Risk
**Medium-High** — primary risk is that community solar generation kWh simply isn't
exposed per account, only the net bill credit. If true, this utility is a dead end
for NEPOOL-GIS reporting which requires raw MWh, not just dollar credits.

## References
- VEC community solar page: https://vermontelectric.coop/co-op-community-solar
- NISC SmartHub: https://www.nisc.coop/
- Generic API docs: https://apidoc-en.smh.smarthing.com/
