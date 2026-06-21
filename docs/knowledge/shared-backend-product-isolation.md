# Shared-Backend Product Isolation (NEPOOL Operator ⟷ Array Operator)

TWO products ride ONE FastAPI backend + ONE Chrome extension, keyed off
`Tenant.product` ∈ {"nepool" (default), "array_operator"}:
- NEPOOL Operator → nepooloperator.com/accounts  (the React SPA in
  solar-operator/web/app, served at /app → /accounts via Netlify proxy)
- Array Operator  → arrayoperator.com            (separate repo /root/array-operator,
  public/*.html, Netlify)

They SHARE the same React bundle at /accounts (AO's /accounts path proxies to the
NEPOOL SPA) and the same auth endpoints. So isolation is NOT structural — it is
enforced per-seam by passing/checking `product`. When a feature "leaks" from one
product into the other, the cause is almost always a seam that forgot to branch
on product. Grep for the seams below FIRST.

## The product-branding infra that ALREADY exists (use it, don't reinvent)
- Backend `api/branding.py`: `brand_name(product)`, `app_url`, `dashboard_url`,
  `magic_link_url(product, token)`, `from_address`, `pricing_blurb`. ALL key off
  `_key(product)`. magic_link_url → NEPOOL `/accounts/?token=`, AO `/login?token=`.
- Backend `api/email_skin.py`: `_theme(product)` → NEPOOL light / AO dark skin.
- Frontend `web/app/src/lib/brand.ts`: `brandFor(account.product)` → wordmark,
  tab labels (NEPOOL "Automatic Reports"/"Clients"/"Master account" vs AO
  "Billing"/"Customers"/"Account"), marketing URL. DashboardLayout already brands
  tab LABELS by product; the bug surface is usually the route CONTENT, not labels.
- `account.product` is returned by GET /v1/account and drives the SPA shell.

## SEAM 1 — Shared route content must dispatch on product, not hardcode one
PITFALL (happened Jun'26): a "redesign Reports tab for billing operators" commit
OVERWROTE `web/app/src/screens/ReportsTab.tsx` wholesale with the Array Operator
billing-run UI — no product branch. The shared `/reports` route then served the
AO billing surface to EVERYONE, clobbering the NEPOOL quarterly-report surface.
Backend was untouched (NEPOOL `/v1/account/reports`, regenerate, next-run,
email-template, scheduler `deliver_quarterly_reports` all intact and isolated
from AO's separate `deliver_billing_reports` path).

FIX PATTERN (clean, recoverable because old components were orphaned not deleted):
1. `git mv ReportsTab.tsx → BillingReportsTab.tsx` (rename export, keep AO UI).
2. Restore the pre-clobber NEPOOL surface from git (`git show <clobber>^:path`)
   as `NepoolReportsTab.tsx` (rename export). The 8 NEPOOL report components
   (QuarterCard, ReportsEmptyState, StatusPill, FailureStrip, NextRunCard,
   AutoReportsSettingsCard, AddCustomerCard, EmailTemplateStudio) usually still
   exist on disk — the clobber just stopped importing them.
3. New thin `ReportsTab.tsx` dispatcher: lazy-load both, branch on
   `account?.product === "array_operator" ? <BillingReportsTab/> : <NepoolReportsTab/>`,
   wrapped in <Suspense>. Default (incl. null product) → NEPOOL.
TEST PITFALL: a lazy/Suspense dispatcher does NOT resolve synchronously in
vitest. Point the OLD surface's test directly at NepoolReportsTab (not the
dispatcher), and write a SEPARATE dispatcher test that mocks both child surfaces
with data-testid and asserts the routing decision only (use waitFor for the lazy
resolve). Verify all 3: nepool→nepool, null→nepool, array_operator→billing.

## SEAM 2 — Auth: magic link / password login must be product-scoped BOTH WAYS
PITFALL (the reported bug): a NEPOOL operator's sign-in link logged them into
Array Operator. Root cause = ASYMMETRY:
- AO's login (`array-operator/public/login.html` + `onboarding.html`) ALREADY
  passed `product:"array_operator"` on /v1/auth/request, /password-login, /verify.
- The NEPOOL SPA `requestLoginLink`/`passwordLogin` in `web/app/src/lib/api.ts`
  passed NO product → request arrived product-blind.
- `issue_magic_link(product=None)` then picked the ACTIVE, NEWEST tenant across
  BOTH products; if the AO tenant was newer it minted an AO token and
  `magic_link_url` sent them to arrayoperator.com.

The backend endpoints (`/v1/auth/request`, `/password-login`) ALL already ACCEPT
an optional `product` to disambiguate — only the NEPOOL client side never sent it.

FIX (steps Ford approved — minimal, symmetric, NO schema change; session itself
is already safe because it encodes tenant_id which encodes product — the leak is
purely WHICH tenant is selected at link-issue time):
1. NEPOOL SPA declares its product. The /accounts bundle IS the NEPOOL dashboard,
   so hardcode `const PRODUCT = "nepool"` in api.ts and pass it on
   requestLoginLink + passwordLogin. Now symmetric with AO.
2. `api/account.issue_magic_link`: STRICT scoping — when `product` is given,
   resolve ONLY within that product; if no tenant matches, return False / send
   NOTHING. NEVER fall back to the other product (the old code kept a
   cross-product candidate and only overrode it if a scoped one existed → the
   leak). Keep the active/newest fallback ONLY for the product-blind (legacy)
   caller path.
3. Bonus: `api/onboarding.py complete()` → `issue_magic_link(email, product=product)`
   (product is in scope from `t.product`) so a dual-product email gets THIS
   account's link, not whichever is newest.

DUAL-ACCOUNT REALITY (Ford confirmed these exist): one email legitimately owns a
NEPOOL tenant AND an AO tenant. Strict scoping is CORRECT for them: NEPOOL login
→ NEPOOL tenant, AO login → AO tenant, no cross-contamination. `password_login`
already verifies the password against EVERY tenant for the email and ranks by
(requested-product, active, newest) — don't regress that to first().

TEST RECIPE (tests/test_password_auth.py::TestMagicLinkProductScoping): make a
dual-account email with the AO tenant created LAST (so it sorts first under
active/newest — the worst case). Assert: nepool request → nepooloperator.com and
"arrayoperator.com" NOT in html; AO request → arrayoperator.com; nepool request
when only an AO tenant exists → returns False, emails NOTHING (capture dict stays
{}); product-blind caller → legacy fallback preserved. Monkeypatch
`account._send_via_resend` to capture html/text. Run with
`source venv/bin/activate && python -m pytest tests/test_password_auth.py -q`.

## SEAM 3 — "login looks broken" is usually STICKY/STACKED session-expiry toasts
PITFALL (Jun'26, presented as a login bug with a correct password): screenshot
showed TWO stacked "session expired" toasts (one info, one red) on the login
screen and a correct password seemingly not working. NOT a credential bug — it
was 401 error-toast handling. Three compounding causes:
1. One expired session fans out into several concurrent authed requests
   (`/v1/account`, `listClients`, reports…); EACH 401 independently dispatched
   `UNAUTHORIZED_EVENT` → a STACK of identical toasts.
2. Some screens caught the 401 and did `toast.error(err.message)` WITHOUT
   filtering `UnauthorizedError`, surfacing a scary RED "Session expired" —
   unlike the well-behaved screens (`ArrayOverview`, `DashboardLayout`) that
   already do `if (err instanceof UnauthorizedError) return`. (`ClientsSection`,
   the /clients landing, was the offender.)
3. Error/warning toasts are DISMISS-ONLY (web/app/src/ui/Toast.tsx never
   auto-clears them) and `ToastProvider` sits ABOVE the router → the red toast
   lingered over login AND survived the next successful sign-in. THAT is why a
   working login *looked* broken.

FIX (centralized — do NOT whack-a-mole each call site):
- `web/app/src/lib/api.ts`: replace the ~12 duplicated inline 401 blocks
  (`clearSession(); window.dispatchEvent(UNAUTHORIZED_EVENT); throw new
  UnauthorizedError()`) with ONE `notifyUnauthorizedOnce()` guarded by a
  module-level `unauthorizedNotified` flag (clears session, dispatches at most
  once). `setSession()` resets the flag so the NEXT genuine expiry bounces again.
- Any screen that auto-loads on mount must import `UnauthorizedError` and ignore
  it in its catch. When this bug class appears, AUDIT every mount-effect/loader
  catch — the bug is the ones that DON'T filter it.
- Add `clear()` to the Toast API and call `toast.clear()` in App.tsx's
  `UNAUTHORIZED_EVENT` handler BEFORE showing the single info toast → login
  starts clean, nothing sticky survives sign-in.

TEST RECIPE (web/app/src/__tests__/auth401.test.ts): stub global `fetch` → a 401
Response (json detail + clone()), arm via setSession, then assert (a) 3
concurrent `getAccount()` → exactly ONE `UNAUTHORIZED_EVENT` and each rejects
with `UnauthorizedError`; (b) after a fresh `setSession`, a later expiry bounces
AGAIN (flag re-armed). `getAccount` routes through the shared `request()` so it
exercises the real path.

NOTE: this diagnosis was code+screenshot only (Ford clamps live prod-auth
probing). If after deploy a CORRECT password still 401s at /v1/auth/password-login
itself, THAT is a separate backend issue — chase the real network response then;
don't claim "verified" when live E2E was blocked.

## General rule for this codebase
Whenever a feature "shows up in the wrong product" or "logs into the wrong brand":
it is a product-branch seam that's missing or asymmetric. Check, in order:
(a) the shared React route content (dispatch on account.product),
(b) the auth client call (does it pass product? is BOTH sides symmetric?),
(c) issue_magic_link / branding.magic_link_url scoping,
(d) email skin / from_address. The backend usually already supports product —
the gap is a client or a selection query that forgot to use it.
