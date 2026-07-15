# Palmetto Electric Cooperative (SC) Adapter Plan

**Status**: bespoke (Meridian Cooperative OnlinePortal)
**Portal**: https://epayment.palmetto.coop/onlineportal/Customer-Login
**Evidence**: Not present in SMARTHUB_HOSTS or providers/*.csv; confirmed non-SmartHub via prior identification.

## Required next step
Capture real logged-in HAR at epayment.palmetto.coop while performing login (account number + password) + billing/usage data pull. Do not fabricate endpoints.

## Adapter constraints
- Login uses account number/user ID + password (no SmartHub/NISC path).
- Reverse-engineer only from captured traffic.
- Do not promote to registry or mark `added` without HAR evidence.

File created per Sovereign rules for research-only bespoke case.