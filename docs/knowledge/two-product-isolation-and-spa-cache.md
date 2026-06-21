# Two products, one backend: isolation + the SPA-shell cache trap

NEPOOL Operator (nepooloperator.com) and Array Operator (arrayoperator.com) share
ONE FastAPI backend, one `Tenant` table, one Chrome extension, AND one served SPA
bundle (`api/app_dist`, mounted at both `/app` and `/accounts`). `Tenant.product`
("nepool" | "array_operator") is the isolation key. Two recurring failure classes
live here.

## 1. The SPA-shell cache trap — "I deployed the fix but it STILL reproduces"

THE most time-wasting trap on this stack. Symptom: you ship a correct frontend
fix (verified in the source AND in the built bundle AND prod is serving the new
`index-HASH.js`), but the user's browser keeps showing the OLD behavior — a
screenshot byte-identical to the pre-fix bug.

ROOT CAUSE: `index.html` (the SPA shell) was served with NO `Cache-Control`
header. Browsers cache it, so on every visit they load the OLD shell, which
references the OLD content-hashed JS chunks → user runs last deploy's code. A
fixed bug looks unfixed because the browser never fetches the new shell.

BEFORE concluding "the fix didn't work," RULE OUT CACHE FIRST:
- `curl -sD - -o /dev/null https://nepooloperator.com/accounts/ | grep -i cache-control`
  — if the shell has no `no-cache`, that's your bug, not the code.
- Confirm prod serves your bundle: `curl -s .../accounts/ | grep -o 'index-[A-Za-z0-9_-]*\.js'` vs `api/app_dist/index.html`.

THE FIX (shipped, `SPAStaticFiles.get_response` in `api/app.py`): stamp
`Cache-Control: no-cache, must-revalidate` on the shell (index.html — served
directly, as a deep-link, or as the 404 fallback; detect via
`path in ("","/","index.html") or content-type startswith text/html`). Leave
content-hashed `/assets/*` alone — their filenames change every build so caching
them forever is correct + fast. Verify with a Starlette `TestClient`: `/accounts/`,
`/accounts/index.html`, `/accounts/clients` → `no-cache`; a hashed asset → unchanged.

INTERIM for the user on a stale browser: ONE hard refresh (Ctrl/Cmd+Shift+R)
pulls the new shell. After the no-cache deploy, future updates need no refresh.

PITFALL: do NOT assume a hard-refresh recommendation alone is the fix — fix the
header so it never recurs for anyone. The refresh is the stopgap.

## 2. Product-scoped auth — a NEPOOL login must NEVER land in Array Operator

Infra already exists: `Tenant.product`, `api/branding.py` (`magic_link_url`,
`brand_name`, per-product `app_url`/`dashboard_url`), and all three auth
endpoints accept a `product` param. The leak was ASYMMETRIC: AO's
`array-operator/public/login.html` passed `product:"array_operator"`, but the
NEPOOL SPA's `requestLoginLink`/`passwordLogin` in `web/app/src/lib/api.ts` did
NOT — so the request arrived product-blind and `issue_magic_link` picked the
ACTIVE/NEWEST tenant across BOTH products, emailing some operators an
`arrayoperator.com` link.

THE FIX (both halves required — fixing one alone leaves the leak):
- Frontend: the `/accounts` bundle IS the NEPOOL dashboard, so hardcode
  `const PRODUCT = "nepool"` and send it on `/v1/auth/request` + `/password-login`.
- Backend `issue_magic_link`: when a product is given, resolve ONLY within that
  product; if no tenant matches, REFUSE (return False, send nothing) — never fall
  back cross-product. Product-blind callers keep the active/newest fallback.
  Onboarding passes the just-created `product` too.

Dual-account emails (same email owns a NEPOOL AND an AO tenant) are REAL and
expected (Ford confirmed). Strict scoping is exactly right: NEPOOL login → NEPOOL
tenant, AO login → AO tenant, no cross-contamination. Verify in
`tests/test_password_auth.py` (`TestMagicLinkProductScoping`): worst case = make
the AO tenant LAST so it sorts first under (active, created_at desc), then assert
a `product="nepool"` request still targets `nepooloperator.com/accounts/?token=`.

PITFALL: `/v1/auth/verify` doesn't (need to) check product — the token is
tenant-bound so the session is correct; the routing decision happens at
issue-time. Don't over-engineer verify.

## 3. Shared SPA, product-branched UI (the Reports clobber)

The served React bundle renders for BOTH products; branch UI on `account.product`
(`brandFor()` in `web/app/src/lib/brand.ts` already brands the shell/tab labels).
A redesign that REPLACES a shared screen's content wholesale (commit 128d428
overwrote `ReportsTab.tsx` with the AO billing run) silently clobbers the other
product. PATTERN: make the shared route a thin dispatcher — `ReportsTab.tsx`
branches `account.product === "array_operator" ? <BillingReportsTab/> :
<NepoolReportsTab/>`, both lazy-loaded. Backend stayed isolated the whole time
(`deliver_quarterly_reports` vs `deliver_billing_reports`); damage was
frontend-only and the old NEPOOL components were still on disk (orphaned, not
deleted) — check that before resurrecting from git history.

## 4. Session-expiry UX: dedupe the 401 bounce, don't strand a sticky red toast

A single expired session fans out into several concurrent authed requests
(account + clients + reports), each returning 401. Without a guard, EACH
dispatches `UNAUTHORIZED_EVENT` → a STACK of "session expired" toasts; and error
toasts are dismiss-only (never auto-clear) so a red one lingers over the login
screen and even survives the next successful sign-in — making a WORKING login
look broken. FIX: one-shot `notifyUnauthorizedOnce()` in `api.ts` (re-armed in
`setSession`); screens that auto-load on mount must ignore `UnauthorizedError`
(the established `ArrayOverview` pattern — `ClientsSection` was the offender);
add `toast.clear()` to the global unauthorized handler so login starts clean.
NOTE: this presents identically to the §1 cache trap (stale toast handling), so
when the user says "still broken" with the same screenshot, check the cache FIRST.

CRITICAL SUB-CASE — a 401 is NOT always a session expiry. `request()` treated
EVERY 401 as expiry, so a WRONG/UNSET PASSWORD on `/v1/auth/password-login`
showed the misleading double "Session expired" toast + bounce instead of the
real error. A 401 on a `noAuth` request (password-login, auth/verify,
auth/request) means BAD CREDENTIALS — there's no session to expire. FIX: in
`request()`, only fire `notifyUnauthorizedOnce()` + throw `UnauthorizedError`
when `!opts.noAuth`; for a noAuth 401, throw a plain Error carrying the server's
message (`parseError(res)`) so `Login.tsx` shows "Invalid email or password"
inline. After this, when password login still fails the user sees the TRUE
reason — and "Invalid email or password" with a correct-looking password means
that tenant has no matching password_hash (offer the magic-link tab or a
password reset; do NOT make `password_login` filter by product — it checks the
pw against every tenant on the email and only RANKS by product).

## 5. Lifecycle state changes MUST be enforced by a UI gate, or they "do nothing"

Symptom (Ford, NEPOOL): "Cancel my trial didn't do anything — it should cancel
the account." DIAGNOSIS-FIRST PAYOFF: before touching code, inspect the tenant in
prod (`railway ssh "... db.query(Tenant).filter(contact_email.ilike(...)) ..."`).
The backend `POST /v1/onboarding/cancel-trial` HAD already flipped the tenant to
`active=False, subscription_status="cancelled"` (note: two l's; the Stripe webhook
`_process_subscription_deleted` writes "canceled", one l — match BOTH spellings
everywhere). The endpoint worked. The defect was purely that the SPA never
ENFORCED the cancelled state: `password_login` mints a session regardless of
status, and `tenant_from_session`/`/v1/account` DELIBERATELY let inactive tenants
through (so they can see status / export). So the full working dashboard still
loaded → cancelling read as a no-op.

CLASS LESSON (Ford bar): a state change with no enforced, visible consequence
reads as a defect. A backend truth (`active=False`) that the UI doesn't honor is
the same bug class as §4 (a 401 the client mishandles). When a "do X" action
flips the DB but the user says it "did nothing," the gap is almost always the
ENFORCEMENT/UI layer, not the action.

THE FIX (mirror the existing pattern — DON'T invent a new one): the stack already
hard-gates `paused_no_card` via `TrialEndedGate` rendered in `DashboardLayout`
instead of `<Outlet/>`. Add a sibling `CancelledGate.tsx` and gate it the same way:
```
const cancelled = account?.active === false &&
  (account?.subscription_status === "cancelled" ||
   account?.subscription_status === "canceled");
// in <main>:  cancelled ? <CancelledGate/> : pausedNoCard ? <TrialEndedGate/> : <Outlet/>
```
Also suppress trial/heartbeat banners when `cancelled` so the gate is the single
surface. Guard on `active === false` so a re-subscribed tenant (status flipped
back) is never gated.

HONESTY PITFALL: do NOT wire an auto add-card "reactivate" button on the cancelled
gate. The auto-resume/`addPaymentMethod` path is built ONLY for `paused_no_card`
(see `resume-from-pause`). Promising add-card reactivation on a cancelled account
is a fake button — make reactivation an honest human step ("email us to
reactivate"). Name the gap to Ford rather than fabricating the flow.

OPEN CHOICE to surface, not silently decide: a cancelled tenant can still LOG IN
(they just hit the gate). If Ford wants login itself refused (401 "account
cancelled"), that's a separate `password_login` hardening — offer it, don't
assume it, because a returning customer may need login to reach the reactivate
path.

### 5a. CRITICAL: the React-SPA fix does NOT cover Array Operator (different frontend)

The §5 `CancelledGate` lives in the React SPA (`web/app/src/screens/DashboardLayout.tsx`),
which is what `/accounts` proxies to. BUT Array Operator owners do NOT live in that
SPA — `arrayoperator.com/` is a SEPARATE plain-JS Netlify app at
`/root/array-operator/public/` (`index.html` + `app.js` + `sandbox.js`, no build
step). The whoami chip links to the in-page `#account` tab (owned by sandbox.js),
NOT to `/accounts`. So a NEPOOL/React fix to a lifecycle gate, danger-zone control,
or any owner-facing UI carries over to AO for ZERO. This is the central trap of
the cross-product parity ask: "is X ready for Array Operator?" almost always means
"go re-implement X in the plain-JS app," not "it's shared, done."

ALWAYS, when asked to bring a NEPOOL owner-facing feature to AO: confirm WHICH
surface AO owners actually use (plain-JS `public/` vs the shared `/accounts` SPA)
before claiming parity. Do a per-issue breakdown — some NEPOOL fixes are
backend-only (carry over free), some are React-only (don't), some don't apply at
all (e.g. the GMP reauth email is NEPOOL-only since AO has no GMP).

AO cancel/lockout parity (shipped this session — reproduce this shape):
- LOCKOUT GATE in `app.js`: `aoIsCancelled(a)` (`a.active===false && status in
  {cancelled,canceled}`) + `aoShowCancelledGate()` that appends a full-viewport
  fixed overlay (`z-index:2147483647`, inline styles, `document.body.style.overflow
  ="hidden"`) with the SAME honest copy as the React gate ("account is cancelled…
  data is safe… email us to reactivate" + Sign out). Expose both on `window` so
  sandbox.js can call them. TRIGGER from BOTH authoritative `/v1/account` reads:
  the boot-time whoami fetch in app.js (`.then(a => { if(aoIsCancelled(a))
  aoShowCancelledGate(); ...})`) AND `loadAccount()` in sandbox.js — defense in
  depth so no surface (arrays/reports/account) loads working behind the gate.
- CANCEL CONTROL in sandbox.js `renderAccountList`: a `cancelRow(a)` appended to
  the flat acct-row list, shown ONLY when trialing (mirror the React DangerZoneCard
  gating — `status==="trialing"`); inline confirm (Cancel → Keep / Yes); POST the
  SAME shared `/v1/onboarding/cancel-trial`; on success hand off to
  `window.aoShowCancelledGate()`. Post-trial accounts cancel via the existing
  Stripe billing portal (Payment-method row) — don't add a redundant button.

AO DEPLOY for this (NOT the React build loop): AO is plain static files on Netlify.
After editing `public/*.js`, `node --check` each file, then deploy with
`python3 /root/array-operator/scripts/netlify_api_deploy.py` (the Netlify CLI is
unreliable here — see ao-deploy-and-frontend-debugging.md). VERIFY live by cur/grep
of the served asset (`curl -s https://arrayoperator.com/app.js | grep -c
aoShowCancelledGate`), then commit+push the AO repo separately (it has its own
GitHub remote). There is NO `build_app.sh` / `api/app_dist` step for AO — that's
NEPOOL-only.

## Deploy reminder for this repo

Ford's standing rule: ALWAYS commit AND `git push origin HEAD:main` so he can
test — don't leave a verified fix sitting uncommitted in the working tree (this
session, the auth fix was built but never pushed, so prod ran old code and the
bug "persisted"). Shared hot tree: stage ONLY your hunks (explicit paths), never
`git add -A`. Frontend changes require `bash build_app.sh` to refresh
`api/app_dist` BEFORE committing, or prod serves the old bundle.
