# Adapter Plan: Palmetto Electric Cooperative (SC)

**Status**: bespoke (Meridian Cooperative OnlinePortal)
**Portal**: https://epayment.palmetto.coop/onlineportal/Customer-Login
**SmartHub host**: -

## Evidence
- Explicitly identified as non-SmartHub in request metadata (family=bespoke, high confidence).
- Login uses account number/user ID + password (no SmartHub JSON flows).
- No CSV entry or smarthub_host present; cannot be promoted via registry.

## Next Step (HAR required)
Capture real logged-in session traffic as .HAR during sign-in + billing/usage pull. Reverse-engineer auth + data endpoints from Meridian OnlinePortal only after capture. Do not fabricate endpoints or mark 'added'.

No code changes; research-only artifact.