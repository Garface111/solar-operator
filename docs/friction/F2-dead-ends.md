# Friction Comb — Lens 2: Dead Ends & Escape Hatches

READ-ONLY audit. No code changed. Scope: every loading / empty / error / waiting /
"what now?" state across the two SPAs (`web/onboarding`, `web/app`) and shared UI.

Method: walked every screen + its async paths, traced what the operator sees when
each promise resolves to nothing, fails, or hangs, and whether there is a visible
path forward (button, link, retry, support contact, auto-advance).

---

## Executive Summary

**33 states audited.**

| Risk | Count | Meaning |
|------|-------|---------|
| **Blocker** | 1 | No path forward at all; UI actively misleads |
| **High** | 5 | Silent failure or a stated path that doesn't actually work |
| **Medium** | 11 | A path exists but it's manual, unexplained, or easy to miss |
| **Low** | 16 | Clear, visible path forward (good state) |

The onboarding wizard's **session-token loss** is the dominant theme: three separate
screens (`Extension`, `Clients`, and indirectly `Done`) depend on a token in
`sessionStorage`/URL, and when it's gone they print "please restart from the welcome
screen" with **no link to do so** and a primary button that **silently no-ops**. The
dashboard side is much healthier — almost every action toasts on failure and re-enables
its button — but it leans hard on disappearing 5s toasts and a "Refresh to try again"
instruction with no actual retry control.

### Top 5 most exposed dead-ends

1. **Extension screen, lost onboarding session** (Blocker) — `web/onboarding/src/screens/Extension.tsx:90-96,149-151,304-306`. Tells the user to "restart from the welcome screen" but renders no link, never starts polling, and the only button (`I've installed it →`) returns early doing nothing. Fully stuck.
2. **Magic-link expired / invalid → silent blank login** (High) — `web/app/src/App.tsx:55-68`. A bad/expired link is caught and swallowed; the user is dropped on an empty login form with zero explanation of why they aren't signed in. Looks broken.
3. **Activation code never loads after webhook lag** (High) — `web/onboarding/src/screens/Extension.tsx:47-75,209-211`. After ~20 retries (60s) the code field stays on `"Loading…"` forever with no error and a permanently-disabled Copy button. The operator can advance but never got the code the extension needs — silently un-linkable.
4. **Clients (onboarding), lost session on Finish** (High) — `web/onboarding/src/screens/Clients.tsx:122-128,329-331`. A fully-filled multi-client form; on submit with no token it shows "restart from the welcome screen" with no link, and `Finish setup` silently returns. Work is stranded.
5. **Chrome Store link may 404 behind a stale "unpublished" guard** (High) — `web/onboarding/src/screens/Extension.tsx:17,164,189-194`. The "still pending publication" warning only renders when the URL starts with `#`, but the URL is now a real store path (CLAUDE.md notes the extension is "v1.0.1 pending Chrome Store push"). If the listing isn't live, the user clicks into a dead store page with no warning.

---

## Loading States

### State 1 — Dashboard initial auth / boot spinner
**Where it surfaces:** `web/app/src/App.tsx:96-102` — `AuthState === "loading"` while `AuthGate.boot()` runs (magic-link exchange + session check).
**What the user sees:** A bare centered 6×6 spinner on an otherwise empty page. No text, no logo, no "Signing you in…".
**Escape hatches available:** None during load. `boot()` always resolves to `authed`/`anon`, *unless* `verifyLoginToken` hangs on a stalled network — there is no fetch timeout (`api.ts:61`), so the spinner can spin indefinitely.
**Dead-end risk:** Medium
**Recommended addition:** Add a short label ("Signing you in…") and a timeout fallback (e.g. after ~10s show "Taking longer than expected — reload or sign in again" with a link to `/login`).
**Files involved:** `web/app/src/App.tsx:42-102`, `web/app/src/lib/api.ts:53-90`

### State 2 — `/v1/account` fetch (dashboard chrome)
**Where it surfaces:** `web/app/src/screens/DashboardLayout.tsx:37-57`; rendered by `AccountTab.tsx:9-21` and `ReportsTab.tsx:9-21`.
**What the user sees:** A 6×6 spinner centered in the tab body (`account === null && !failed`). TopNav + TabBar are already painted, so the shell is navigable.
**Escape hatches available:** Good — TabBar and Sign out are outside the loading region, so the user can switch tabs (Clients loads independently of the account fetch, per the comment at `DashboardLayout.tsx:26-31`) or sign out. No per-fetch timeout, though.
**Dead-end risk:** Low
**Recommended addition:** None critical; optionally a skeleton instead of a bare spinner for perceived speed.
**Files involved:** `web/app/src/screens/DashboardLayout.tsx:32-79`, `AccountTab.tsx`, `ReportsTab.tsx`

### State 3 — Clients list loading
**Where it surfaces:** `web/app/src/components/ClientsSection.tsx:82-88` (`clients === null`).
**What the user sees:** A card with a 4×4 spinner + "Loading clients…". "Import spreadsheet" and "+ Add client" buttons are already live above it.
**Escape hatches available:** Good — header actions and tab nav remain usable.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ClientsSection.tsx:57-106`

### State 4 — Arrays-under-a-client loading
**Where it surfaces:** `web/app/src/components/ArrayList.tsx:70-77` (`arrays === null`).
**What the user sees:** Inline "Loading arrays…" with a small spinner inside the expanded client card.
**Escape hatches available:** Good — the rest of the card and the page stay interactive; the client can be collapsed.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ArrayList.tsx:25-118`

### State 5 — Provider list loading (link a utility account)
**Where it surfaces:** `web/app/src/components/ArrayList.tsx:365-369`.
**What the user sees:** The provider `<select>` briefly empty, then populated; on fetch failure it falls back to a hardcoded `[{gmp}]`.
**Escape hatches available:** Excellent — graceful degradation to the GMP default, no blocking state.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ArrayList.tsx:348-369`

### State 6 — In-app save spinners (frequency, cc toggle, email settings, editable fields, add/delete)
**Where it surfaces:** Buttons swap to `<Spinner/> + "Saving…/Sending…/Deleting…"` across `ReportsCard.tsx:229-238`, `EmailCustomizationCard.tsx:238-247`, `AccountSummaryCard.tsx:142-150`, `EditableField.tsx:92-94`, `AddClientModal.tsx:87-95`, `ArrayList.tsx:415,491`.
**What the user sees:** Disabled button with spinner; modals block their close affordances while in flight (`disabled={saving}`).
**Escape hatches available:** Good — every one has a `catch` that re-enables the control and toasts; no fetch timeout, so a hung request leaves a permanently-disabled button.
**Dead-end risk:** Medium (only because of the missing timeout on a stalled network — otherwise Low).
**Recommended addition:** A client-side fetch timeout in `request()`/`ingestPreview()` so a stalled connection surfaces an error instead of a forever-disabled button.
**Files involved:** `web/app/src/lib/api.ts:53-76`, all card components above

### State 7 — AI ingest "Parsing your spreadsheet…"
**Where it surfaces:** `web/app/src/components/ImportSpreadsheetModal.tsx:211-216` (`stage === "parsing"`).
**What the user sees:** Centered spinner + "Parsing your spreadsheet…". No progress %, no elapsed time, no Cancel button while parsing.
**Escape hatches available:** Partial — Escape key and backdrop click still close the modal during parse (`:50-55` and `:140-142` only block while `committing`, not while `parsing`), but there is **no visible** cancel control and the in-flight LLM call keeps running. A slow/large file or a hung LLM has no timeout.
**Dead-end risk:** Medium
**Recommended addition:** Show a visible "Cancel" button during parse and an explicit timeout that returns to the upload stage with "This is taking unusually long — try a smaller file or add manually."
**Files involved:** `web/app/src/components/ImportSpreadsheetModal.tsx:27-216`, `web/app/src/lib/api.ts:394-416`

### State 8 — ToS / Privacy markdown loading & error
**Where it surfaces:** `web/onboarding/src/ui/MarkdownDoc.tsx:76-100`, used in `Welcome.tsx:82-88`.
**What the user sees:** "Loading Terms of Service…" spinner; on error, "Couldn't load the {title}. You can also read it at this link." with a working external link.
**Escape hatches available:** Excellent — error state offers a direct link to the raw file.
**Dead-end risk:** Low
**Recommended addition:** None — this is the model the rest of the app should follow.
**Files involved:** `web/onboarding/src/ui/MarkdownDoc.tsx:52-109`

---

## Empty States

### State 9 — Zero clients
**Where it surfaces:** `web/app/src/components/ClientsSection.tsx:89-94`.
**What the user sees:** Card: "No clients yet. Add your first reporting client to get started." with "Import spreadsheet" + "+ Add client" buttons above.
**Escape hatches available:** Excellent — two clear primary actions.
**Dead-end risk:** Low
**Recommended addition:** Optionally make the empty card itself a clickable CTA, but not required.
**Files involved:** `web/app/src/components/ClientsSection.tsx:57-119`

### State 10 — Zero arrays under a client
**Where it surfaces:** `web/app/src/components/ArrayList.tsx:81-86`.
**What the user sees:** Dashed box: "No arrays yet. Arrays appear here once GMP auto-populate runs, or add one manually." + "+ Add array".
**Escape hatches available:** Excellent — explains both the passive (auto-populate) and active (manual) path.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ArrayList.tsx:79-118`

### State 11 — Zero utility accounts under an array
**Where it surfaces:** `web/app/src/components/ArrayList.tsx:299-301`.
**What the user sees:** "No utility accounts linked yet." + "+ Link a utility account".
**Escape hatches available:** Good.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ArrayList.tsx:273-346`

### State 12 — No reports sent yet
**Where it surfaces:** `web/app/src/components/ReportsCard.tsx:149-153`.
**What the user sees:** "No reports sent yet" line; "Send a report now" button is present regardless.
**Escape hatches available:** Good.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ReportsCard.tsx:148-177`

### State 13 — Blank NEPOOL-GIS ID on an array
**Where it surfaces:** `web/app/src/components/ArrayList.tsx:170-177` (editable field, empty); also onboarding `Clients.tsx:283-293` (optional field).
**What the user sees:** An empty editable cell with placeholder "NON12345". Nothing flags that a blank ID produces a malformed sheet title (`"<Array Name> ()"` per the GMCS writer rules in CLAUDE.md).
**Escape hatches available:** The field is editable, so there's a path to fix it — but no signal that it *needs* fixing. This is a silent data-integrity gap rather than a navigation dead-end.
**Dead-end risk:** Medium
**Recommended addition:** Show a subtle inline warning badge on arrays missing a NEPOOL-GIS ID (e.g. "Reports will ship without a GIS ID") so the operator isn't surprised by a blank parenthetical in the workbook.
**Files involved:** `web/app/src/components/ArrayList.tsx:169-177`, `api/writers/gmcs_writer.py` (referenced, not read)

### State 14 — Empty editable field (contact email, GMP login, notes)
**Where it surfaces:** `web/app/src/ui/EditableField.tsx:88-91` via `emptyText` ("add contact email", "add GMP login", "—").
**What the user sees:** Muted prompt text that is itself the click-to-edit target.
**Escape hatches available:** Excellent — the empty state *is* the affordance.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/ui/EditableField.tsx:25-130`, `ClientCard.tsx:105-114`

### State 15 — Missing activation code on Account tab
**Where it surfaces:** `web/app/src/components/ActivationCodeCard.tsx:30-34`.
**What the user sees:** Amber card: "No activation code on file. Email support@solaroperator.org and we'll sort it out."
**Escape hatches available:** Good — explicit support contact.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ActivationCodeCard.tsx:12-47`

### State 16 — Done screen with 0 arrays so far
**Where it surfaces:** `web/onboarding/src/screens/Done.tsx:68-73`.
**What the user sees:** "0 arrays so far" stat plus "Arrays from auto-populate clients will appear once they sign into GMP through the extension." + dashboard CTA.
**Escape hatches available:** Excellent — explains the wait and points to the dashboard.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/onboarding/src/screens/Done.tsx:47-83`

---

## Error States

### State 17 — 401 → forced redirect to login
**Where it surfaces:** `web/app/src/lib/api.ts:67-71` (and the duplicated handler in `ingestPreview` at `:409-413`) → `UNAUTHORIZED_EVENT` → `App.tsx:87-94` navigates to `/login`.
**What the user sees:** Whatever they were doing vanishes and they're on the login screen. No toast explaining "Your session expired" — the `UnauthorizedError("Session expired — sign in again")` message is swallowed (DashboardLayout ignores it at `:45`; action-level catches may briefly toast it, but the navigation wins).
**Escape hatches available:** The login screen itself is the path forward (good), but mid-edit work (e.g. a half-written email template) is lost with no warning.
**Dead-end risk:** Medium
**Recommended addition:** On `UNAUTHORIZED_EVENT`, raise a persistent info toast on the login screen ("Your session expired — sign in again to continue") so the redirect doesn't feel like a glitch.
**Files involved:** `web/app/src/lib/api.ts:67-71,409-413`, `web/app/src/App.tsx:86-94`

### State 18 — Generic network / request failure (any dashboard action)
**Where it surfaces:** `request()` throws `parseError` → `"Request failed (status)"`; every caller toasts it. e.g. `ClientCard.tsx:37`, `ArrayList.tsx:38`, `ReportsCard.tsx:54`.
**What the user sees:** A red 5-second auto-dismissing toast (`Toast.tsx:29`), then nothing. The button re-enables.
**Escape hatches available:** Retry by re-clicking the action. But the error message disappears in 5s and there's no persistent error log, so a user who looks away misses it.
**Dead-end risk:** Medium
**Recommended addition:** Make error toasts persist (or be dismiss-only) instead of auto-dismissing at 5s, or surface a small inline error near the failed control.
**Files involved:** `web/app/src/ui/Toast.tsx:28-29,87-90`, `web/app/src/lib/api.ts:34-44`

### State 19 — Account fetch failed (dashboard)
**Where it surfaces:** `DashboardLayout.tsx:43-52` sets `failed=true`; rendered by `AccountTab.tsx:12-18` and `ReportsTab.tsx:12-18`.
**What the user sees:** "Couldn't load your account. Refresh to try again." (plain text) plus the one-time error toast.
**Escape hatches available:** Partial — the *instruction* to refresh exists, but there is **no Retry button**; the user must manually reload the browser. Tab nav still works (Clients tab is independent), and Sign out works, so they aren't fully stuck.
**Dead-end risk:** Medium
**Recommended addition:** Add a "Retry" button next to the message that re-runs `getAccount()` without a full page reload.
**Files involved:** `web/app/src/screens/AccountTab.tsx:9-21`, `web/app/src/screens/ReportsTab.tsx:9-21`, `DashboardLayout.tsx:37-57`

### State 20 — Magic-link expired / invalid
**Where it surfaces:** `web/app/src/App.tsx:55-68` — `verifyLoginToken` throws, caught at `:60` with only a comment ("Bad/expired link — fall through"), token stripped, state resolves to `anon`.
**What the user sees:** The plain login form. **No message** that the link they just clicked was expired or already used. To the operator it looks like the link "did nothing."
**Escape hatches available:** They can request a new link — but nothing tells them they need to, or why.
**Dead-end risk:** High
**Recommended addition:** When the token exchange fails, carry a flag to the login screen and show "That sign-in link has expired or was already used. Enter your email for a fresh one." (The login screen already supports a sent/empty state — `Login.tsx:50-110` — so this is a small surface.)
**Files involved:** `web/app/src/App.tsx:54-71`, `web/app/src/screens/Login.tsx`

### State 21 — Stripe checkout unreachable (create-checkout fails)
**Where it surfaces:** `web/onboarding/src/screens/Info.tsx:47-54`.
**What the user sees:** Red toast "Couldn't reach checkout. Check your connection and try again."; the "Continue to checkout →" button re-enables.
**Escape hatches available:** Good — clear retry, Back button still present.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/onboarding/src/screens/Info.tsx:34-55`

### State 22 — Stripe checkout cancelled (bailed on Stripe)
**Where it surfaces:** `web/onboarding/src/screens/Info.tsx:22-24,68-72` (`?cancelled=1`).
**What the user sees:** Amber banner "Checkout was cancelled. No charge was made — you can try again below." Form is intact.
**Escape hatches available:** Excellent — reassures (no charge) and re-offers the path.
**Dead-end risk:** Low
**Recommended addition:** None — exemplary.
**Files involved:** `web/onboarding/src/screens/Info.tsx:22-72`

### State 23 — AI ingest: no arrays detected
**Where it surfaces:** `web/app/src/components/ImportSpreadsheetModal.tsx:64-70`.
**What the user sees:** Returns to the upload stage with red text: "We couldn't find any arrays in that file. Try a different file, or add clients manually."
**Escape hatches available:** Excellent — names two alternatives, keeps the modal usable.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ImportSpreadsheetModal.tsx:59-81`

### State 24 — AI ingest: file parse error
**Where it surfaces:** `web/app/src/components/ImportSpreadsheetModal.tsx:73-80`.
**What the user sees:** Back to upload stage with "Couldn't parse that file. Try a different file, or add manually." (or the server's detail message).
**Escape hatches available:** Good.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ImportSpreadsheetModal.tsx:59-81`

### State 25 — AI ingest: commit failure
**Where it surfaces:** `web/app/src/components/ImportSpreadsheetModal.tsx:128-134`.
**What the user sees:** Toast "Couldn't import — try again"; stays on the preview table with all edits intact; button re-enables.
**Escape hatches available:** Excellent — no data loss, clean retry.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/ImportSpreadsheetModal.tsx:113-135`

### State 26 — Report send-now failure
**Where it surfaces:** `web/app/src/components/ReportsCard.tsx:86-99`.
**What the user sees:** Toast "Couldn't send the report"; confirm modal stays open for retry.
**Escape hatches available:** Good for total failure. **Gap:** a *partial* send (some clients delivered, some failed server-side) cannot be expressed by this single boolean path — the UI shows either full success or full failure, so a partial failure may be misreported as success.
**Dead-end risk:** Medium
**Recommended addition:** Have `/v1/account/send-report` return per-client results and surface "Sent to 4 of 5 — 1 failed (no contact email)" so the operator can act on the gap.
**Files involved:** `web/app/src/components/ReportsCard.tsx:86-99,214-244`, `web/app/src/lib/api.ts:217-219`

### State 27 — Billing portal open failure
**Where it surfaces:** `web/app/src/components/AccountSummaryCard.tsx:76-87`.
**What the user sees:** Toast "Couldn't open the billing portal"; button re-enables. (Success does a full `window.location.href` redirect away to Stripe.)
**Escape hatches available:** Good.
**Dead-end risk:** Low
**Recommended addition:** None.
**Files involved:** `web/app/src/components/AccountSummaryCard.tsx:59-152`

---

## Waiting States

### State 28 — Extension installed but no capture yet (the core waiting state)
**Where it surfaces:** `web/onboarding/src/screens/Extension.tsx:113-147,263-302`.
**What the user sees:** Pulsing amber dot + "We're waiting for your first GMP capture…", a help hint after 5 ping failures, a pulsing "Having trouble?" button after ~30s (`HELP_THRESHOLD`), a troubleshooting modal with a checklist + `admin@solaroperator.org`, and an always-available "I've installed it →" manual advance.
**Escape hatches available:** Excellent — multiple, escalating, with manual override and human contact. This is the V3 feature done well.
**Dead-end risk:** Low
**Recommended addition:** None — this is the reference pattern for the rest of the app.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:88-147,278-377`

### State 29 — Webhook lag: activation code never arrives
**Where it surfaces:** `web/onboarding/src/screens/Extension.tsx:47-75,209-211`.
**What the user sees:** The code box shows `"Loading…"`. After `MAX_RETRIES = 20` (~60s) the retry loop stops; if the webhook still hasn't fired, the box stays on `"Loading…"` **permanently**, with the Copy button disabled and **no error message**. The operator can still click "I've installed it →" and proceed — but they never got the code the extension requires, so the extension can't authenticate and no captures will ever land.
**Escape hatches available:** None for the actual problem — the failure is invisible. The manual advance leads to a half-broken state, not a fix.
**Dead-end risk:** High
**Recommended addition:** After retries exhaust without a code, replace "Loading…" with "We're still provisioning your account — this can take a minute. Refresh, or email admin@solaroperator.org if it persists," and keep a manual retry button.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:46-75,197-222`

### State 30 — Stripe checkout pending / mid-redirect
**Where it surfaces:** `web/onboarding/src/screens/Info.tsx:110-118` ("Redirecting…") then full-page nav to Stripe; return path via `success_url?onboarding_token=` read in `onboarding.ts:16-23`.
**What the user sees:** "Redirecting…" spinner, then Stripe's hosted page (out of our control), then back on `/extension`.
**Escape hatches available:** Adequate — token is persisted to `sessionStorage` *before* redirect (`Info.tsx:45`) and also returned in the URL, so a same-tab round-trip survives. **Gap:** if the user opens Stripe, abandons it, and later reopens the SPA in a *fresh* tab, `sessionStorage` is gone and there's no "resume checkout" entry point — they'd restart onboarding.
**Dead-end risk:** Medium
**Recommended addition:** Persist the onboarding token in `localStorage` (not just `sessionStorage`) so an interrupted checkout can be resumed across tabs/restarts.
**Files involved:** `web/onboarding/src/screens/Info.tsx:34-55`, `web/onboarding/src/lib/onboarding.ts:6-23`

### State 31 — Chrome Web Store listing not yet live
**Where it surfaces:** `web/onboarding/src/screens/Extension.tsx:17,164,181-194`.
**What the user sees:** A prominent "Install … from the Chrome Web Store ↗" button linking to `CHROME_STORE_URL`. The "still pending publication" warning (`:189-194`) only renders when `storeUnpublished` is true, which is gated on `CHROME_STORE_URL.startsWith("#")` (`:164`). The URL is now a real store path, so the warning is suppressed — but CLAUDE.md states the extension is "v1.0.1 pending Chrome Store push," meaning the link may still 404.
**Escape hatches available:** If the store page is dead, the user lands on Chrome's 404 with no in-app warning and no alternative (no .crx/sideload instructions). They can fall back to "I've installed it →" but they haven't actually installed anything.
**Dead-end risk:** High
**Recommended addition:** Drive the "unpublished" banner off an explicit flag (env/config) rather than a `#`-prefix heuristic, and keep the warning visible until the listing is confirmed live.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:16-17,164,181-195`

---

## "What Now?" States

### State 32 — Just landed on dashboard with nothing set up
**Where it surfaces:** `web/app/src/screens/AccountTab.tsx` / `ClientsTab.tsx` when a tenant reaches the dashboard (e.g. via magic link) before completing setup.
**What the user sees:** Account tab = summary card (zeros) + activation code; Clients tab = the "No clients yet" empty state. There is **no dashboard-level "get started / finish setup" banner** pointing back to onboarding or the extension steps — onboarding owns that flow, but a direct dashboard entry doesn't reconnect to it.
**Escape hatches available:** Partial — the Clients empty state offers "+ Add client"/"Import", and the activation card explains the extension, but nothing orients a brand-new, mid-setup operator on *what to do first*.
**Dead-end risk:** Medium
**Recommended addition:** Show a dismissible "Finish setting up" checklist on the dashboard when `clients_count === 0` or `accounts_count === 0`, linking to add-client / extension steps.
**Files involved:** `web/app/src/screens/AccountTab.tsx`, `web/app/src/components/ClientsSection.tsx:89-94`, `ActivationCodeCard.tsx`

### State 33 — Just requested a magic link ("Check your inbox")
**Where it surfaces:** `web/app/src/screens/Login.tsx:50-74`.
**What the user sees:** Envelope icon, "Check your inbox", the target email, "expires in 15 minutes", and a "Use a different email" link. Support email in the footer (`:113-115`).
**Escape hatches available:** Good — "Use a different email" resets the form, support contact is visible. **Gap:** there is **no "Resend link"** button. If the email never arrives (spam, typo-but-valid address, mail delay), the only options are switch email or email support.
**Dead-end risk:** Low (borderline Medium)
**Recommended addition:** Add a "Didn't get it? Resend" button (with a short cooldown) on the sent state.
**Files involved:** `web/app/src/screens/Login.tsx:16-116`

### State 34 (cross-cutting) — Onboarding wizard, lost session token
**Where it surfaces:** `Extension.tsx:90-96,149-151,304-306` and `Clients.tsx:122-128,329-331` (`getToken()` returns null because `sessionStorage` was cleared / opened in a new tab without the URL param).
**What the user sees:** Red text "We couldn't find your onboarding session. Please restart from the welcome screen." On `Extension`, polling never starts and the page is otherwise idle; on `Clients`, a fully-filled form that won't submit.
**Escape hatches available:** **None that work.** The message says "restart from the welcome screen" but renders no link to `/`. On `Extension`, the only button ("I've installed it →") calls `handleManual`, which returns early at `:151` when there's no token — it silently does nothing. On `Clients`, "Finish setup" returns early at `:123-128`. The user is told to restart but given no way to, and the visible controls are dead.
**Dead-end risk:** Blocker
**Recommended addition:** Render the session-lost message as a real call-to-action — a "Restart setup" button/link to `/` (welcome) — and/or recover the token from `localStorage`. At minimum, never leave a primary button that silently no-ops.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:89-96,149-162,304-306`, `web/onboarding/src/screens/Clients.tsx:120-128,329-331`, `web/onboarding/src/lib/onboarding.ts:6-23`

---

## Cross-cutting observations

- **No fetch timeouts anywhere.** `request()` (`api.ts:53-76`), `ingestPreview` (`:394-416`), and every onboarding `fetch` lack an `AbortController`/timeout. Any stalled connection converts a transient hiccup into a forever-spinner or forever-disabled button (States 1, 6, 7, 29).
- **5-second auto-dismiss on error toasts** (`Toast.tsx:29`) is too aggressive for errors the user must act on (State 18). Success: fine. Error: should persist or require dismissal.
- **"Refresh to try again" with no button** appears in two places (State 19) — instructing a manual browser reload is the weakest escape hatch in the dashboard.
- **The onboarding wizard's reliance on `sessionStorage`** is the single biggest structural dead-end source (States 29, 30, 34). Moving the token to `localStorage` and rendering real recovery CTAs would resolve the Blocker and two Highs at once.
- **The Extension waiting flow (State 28) and the cancelled-checkout banner (State 22) are the gold standard** in this codebase — escalating help, manual override, reassurance, human contact. The fixes above are mostly "make the rest of the app behave like these two."
