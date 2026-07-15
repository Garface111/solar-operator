# Eversource Utility Request Research Plan

**Request**: #7 Eversource (state=unknown, url=unknown)
**Prior directive**: Identify portal family (SmartHub / bespoke). Wire via registry ONLY if SmartHub/NISC with real evidence. For bespoke: capture HAR before any adapter; do not mark 'added' without login evidence.

## Next steps (no code changes yet)
1. Confirm exact customer portal URL(s) via public search (eversource.com login).
2. Inspect login page for SmartHub indicators (smarthub.coop host, /services/oauth/auth/v2 endpoint, or NISC branding).
3. If SmartHub: add host entry to api/data/providers/CT.csv (or MA/NH) + re-run derive; verify via existing smarthub.py registry.
4. If bespoke (most likely): record HAR of real login flow only; draft minimal adapter plan under api/adapters/ but do not implement endpoints.
5. Update utility_requests status only after evidence; never fabricate.

**Status**: research-only artifact shipped. Awaiting HAR or portal evidence.