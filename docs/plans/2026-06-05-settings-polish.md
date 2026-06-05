# Settings Tab Polish

## Why
Settings tab still uses the old visual language post-redesign. The
trial cancel card is there, but the rest is sparse and unstyled.

## Scope
- EDIT: `web/app/src/screens/AccountTab.tsx` (the "settings" tab —
  rename internally if appropriate)
- NEW: `web/app/src/components/settings/*.tsx` for sub-cards

## What's missing
1. **Account section** — name, email, company. Inline edit, save on blur.
2. **Email preferences** — toggle CC-self-on-reports (already exists as
   API field), toggle weekly digest (new), report delivery email
   override.
3. **Utility connections** — list connected portals (GMP / VEC), last
   sync time per provider, "Reconnect" action if a session expired.
4. **Plan & billing** — current plan, next bill date, total monthly cost.
   Already partially in AccountSummaryCard — consolidate here.
5. **Danger zone** — cancel trial / delete account at bottom, separated
   by visual divider.

### Constraints
- Match cream/emerald aesthetic from redesign.
- DO NOT touch billing logic, ONLY surface existing fields.
- Add API endpoints only for fields that don't already have them.
- `./build_app.sh` + commit api/app_dist.

## Tasks
1. Audit current AccountTab.tsx + AccountSummaryCard.tsx for what
   exists.
2. Split into sub-cards under components/settings/.
3. Wire to existing /v1/account endpoints.
4. Build + commit api/app_dist/.
