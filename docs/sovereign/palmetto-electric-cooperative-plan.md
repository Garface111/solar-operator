# Palmetto Electric Cooperative (SC) Adapter Plan

**Status**: Bespoke (Meridian Cooperative OnlinePortal). Not SmartHub.

**Portal**: https://epayment.palmetto.coop/onlineportal/Customer-Login

**Evidence**: Brief states "NOT on SmartHub — it runs a bespoke Meridian Cooperative 'OnlinePortal' (login = account number/user ID + password)". No smarthub_host in providers catalog.

**Next step (no fabricated code)**: Capture real logged-in .HAR at epayment.palmetto.coop (sign-in + billing/usage pull). Reverse-engineer auth + data endpoints from HAR only. Do not mark `added` or implement adapter without verified HAR evidence.

**Do not**: Invent endpoints, promote to SMARTHUB_UTILITIES, or update any registry/CSV.
