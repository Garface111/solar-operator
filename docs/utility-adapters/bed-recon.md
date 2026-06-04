# Burlington Electric Department (BED) — Adapter Recon

**Date:** Jun 3, 2026  
**Status:** Research only — no code written

## Login URL
https://myaccount.burlingtonelectric.com/app/login.jsp  
(Alternate: https://myaccount.burlingtonelectric.com/app/capricorn?para=index)

Credentials: Customer ID + Location ID (from paper bill) + associated phone number.

## Portal Technology
**SpryPoint SpryCIS + SpryEngage** — BED migrated to SpryPoint in January 2025,
replacing the legacy Capricorn/enQuesta system (Systems & Software).

SpryPoint is a cloud-native SaaS CIS vendor with modular products:
- SpryCIS (billing engine)
- SpryEngage (customer portal)
- SpryIDM (interval data management)
- SpryMobile (field ops)

## JSON API?
**No public API documented.** SpryPoint uses "productized integrations" built
by their engineering team, not a self-service developer portal. Any integration
would require direct negotiation with BED and/or SpryPoint.

SpryPoint contact: (855) 879-7779 or info@sprypoint.com.

The previous Capricorn/enQuesta system had some API surface but that is now
decommissioned.

## Community Solar kWh Exposure?
**Uncertain — likely weak.** BED does not have a formal community solar program
documented. Their solar offering is primarily net metering for customer-owned
systems. Whether community solar billing (with per-account generation kWh) is
supported by SpryPoint or BED is not documented anywhere.

SpryIDM handles interval data management which suggests the data pipeline exists,
but exposure to third parties or per-account generation export is not confirmed.

**Could not verify without credentials.**

## Estimated Build Effort
**Hard**

- Brand-new (Jan 2025) platform with no public API, no developer portal
- No documented community solar program — may not be a target market
- Requires direct business partnership with BED + SpryPoint
- 6-12 weeks estimated IF SpryPoint agrees to an integration partnership
- BED contact: 802-865-7300 or netmetering@burlingtonelectric.com

## Risk
**High** — BED is still stabilizing on new platform (6 months in as of this
research). Community solar billing may not exist as a concept in their current
offering. Lowest priority of the 4 utilities surveyed.

## References
- BED billing FAQ: https://www.burlingtonelectric.com/accountfaq/
- BED solar billing: https://www.burlingtonelectric.com/solarbilling/
- SpryPoint case study: https://www.sprypoint.com/resource/sprypoint-adds-burlington-electric-department-to-its-growing-list-of-clients/
