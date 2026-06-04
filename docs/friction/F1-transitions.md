# Friction Comb — Lens 1: Transitions & State Changes

**Audit scope:** Every state transition in the operator journey, flagged for moments where the
operator could plausibly think *"wait, what?"* and the UI/email does not answer.
**Mode:** READ-ONLY. Nothing implemented. Recommendations describe; they do not prescribe code.
**Vector under test (MEGA-VECTOR North Star):** *every screen anticipates what the operator is
about to wonder* — and the competition is the human consultant who "reassured them their report
went out clean."

---

## Executive Summary

**Transitions audited:** 20
**Findings:** 20 (one per transition; several carry multiple sub-findings)

**Count by severity (dominant per transition):**
- **Blocker — 3:** T6 (Chrome Store link), T18 (no delivery confirmation / bounce handling), T20 (paid-but-inactive dead-end)
- **High — 5:** T4, T7, T11, T12, T17
- **Medium — 7:** T1, T3, T8, T10, T13, T15, T16
- **Polish — 5:** T2, T5, T9, T14, T19

**The through-line:** the product *works*, but at the two moments that matter most to a buyer —
*"did my money go through?"* (T4/T20) and *"did my client actually receive the report?"* (T18) —
the UI goes quiet or, worse, gives a false-positive. That is precisely the reassurance the human
consultant sold. We are silent exactly where they were loud.

### Top 5 fixes by buyer-trust ÷ effort

1. **T20 — Kill the "paid but inactive" dead-end (Blocker).** A webhook-lagged operator who clicks
   "I've installed it" gets HTTP 402 *"Complete payment before installing the extension"* — we tell a
   paying customer to pay again. Add a "Confirming your payment…" state, use the `session_id` already
   sitting in the success URL to self-heal, and never 402 a tenant whose Checkout we just redirected
   from. *High trust, medium effort.*
2. **T18 — Make "Send a report now" tell the truth (Blocker-adjacent, tiny effort).** The toast says
   *"Report is on its way to your clients"* on any HTTP 200, even when `deliver_for_tenant` returns
   `{"ok": false, "reason": "no recipient email on file"}` or `email_sent: false`. Inspect the result
   and report partial/failed sends. (The deeper bounce/delivery-receipt gap is bigger — see finding.)
3. **T4 — Add "Payment received ✓" to the Extension screen (High, tiny effort).** A $250 charge just
   happened and the very next screen says only "Install the extension." One reassuring line closes the
   single biggest trust gap in onboarding.
4. **T17 — Surface the magic-link error instead of swallowing it (High, tiny effort).** `App.tsx`
   catches the verify failure and silently falls through; an operator clicking an expired/used link
   lands on a bare login screen with no idea why. The API already returns the exact reason — show it.
5. **T6 — Confirm the Chrome Web Store listing is actually live, or restore the guard (Blocker, tiny
   effort).** `CHROME_STORE_URL` is a real-looking URL and the "still pending publication" warning only
   fires when the URL `startsWith("#")` — so if the listing is not published, every operator clicks
   into a 404 with zero warning, and onboarding is hard-blocked.

> ⚠️ **LOUD CAVEAT:** Two Blockers (T6, T20) depend on **external runtime state** I cannot observe
> from source — whether the Chrome listing is published, and how fast the Stripe webhook lands in
> prod. If the listing is live and the webhook is reliably sub-second, T6/T20 degrade to High. Verify
> both before triaging. Everything else is code-confirmed.

---

## Transition 1 — Landing page → Get Started CTA → Welcome screen

**Operator's likely question at this moment:** "What exactly am I buying, and what does '$45/array'
mean when I have a dozen operator-clients?"
**Does the UI/email answer it?** Partial
**Friction severity:** Medium
**Current behavior:** The marketing landing page is **not in this repository** (Netlify-hosted), so
the CTA copy/handoff cannot be audited here — flagging that as a coverage gap. The Welcome screen
(`Welcome.tsx`) opens with "Quarterly solar reports, on autopilot" and a pricing box that prints the
price **twice and inconsistently**: line 39 `"$250 one-time setup · $45/array/month · cancel anytime"`
immediately followed by line 42 `"from $45/array/month, billed monthly · $250 one-time setup"`. The
service list ("email them to your clients") never explains the tenant→client→array model the buyer
actually lives in.
**Recommended fix:** Collapse the duplicated pricing line into one; add a one-line framing of what an
"array" is and that the per-array price scales across all their clients. Pull the landing page into the
repo (or a sibling) so its CTA→Welcome handoff is auditable.
**Files involved:** `web/onboarding/src/screens/Welcome.tsx:37-44,46-58`; landing page (external, not in repo)

---

## Transition 2 — Welcome (agree checkbox) → Info screen

**Operator's likely question at this moment:** "Am I about to be charged just by continuing?"
**Does the UI/email answer it?** Yes
**Friction severity:** Polish
**Current behavior:** Continue is disabled until the ToS/Privacy checkbox is ticked
(`Welcome.tsx:104`), then navigates to `/info`. The Info screen reassures "Next stop is secure
checkout" (`Info.tsx:64-66`), so the operator knows no charge has happened yet.
**Recommended fix:** None required. Optionally move the "no charge until checkout" reassurance up onto
the Continue button's context so it's seen before the click, not after.
**Files involved:** `web/onboarding/src/screens/Welcome.tsx:94-107`; `web/onboarding/src/screens/Info.tsx:60-66`

---

## Transition 3 — Info (form submit) → Stripe Checkout

**Operator's likely question at this moment:** "I have 10 arrays — why does Checkout only show
$250 + $45 for *one* array?"
**Does the UI/email answer it?** No
**Friction severity:** Medium
**Current behavior:** `checkout()` creates the subscription with the per-array line item hard-coded to
`quantity=1` because the real array count isn't known until Screen 4
(`onboarding.py:119-146,_line_items`). The true quantity is reconciled *after* Screen 4
(`_reconcile_subscription_quantity`). So at the Stripe Checkout the operator sees a total that
under-represents what they'll actually pay, with no explanation that it will be trued-up.
**Recommended fix:** Add a one-line note before the redirect ("You'll confirm your exact array count
in a moment; today's charge covers setup + your first array, and we adjust the monthly total to your
real count after setup"). Or move array entry before Checkout so the quantity is correct up front.
**Files involved:** `web/onboarding/src/screens/Info.tsx:34-55`; `api/onboarding.py:119-146,289-347`

---

## Transition 4 — Stripe Checkout (success) → Extension screen

**Operator's likely question at this moment:** "Did my $250 payment actually go through?"
**Does the UI/email answer it?** No
**Friction severity:** High
**Current behavior:** The success URL lands on `/extension` (`onboarding.py:195-199`). The Extension
screen's first words are "Install the Solar Operator Sync extension" (`Extension.tsx:171`). There is
**no payment-confirmation banner** — no "Payment received," no amount, no receipt pointer. The only
implicit signal is the activation code eventually appearing (and that polls async, often showing
"Loading…"). For a high-ticket one-time charge this is the single largest trust gap in onboarding.
**Recommended fix:** Render a "Payment received ✓ — $250 setup charged, receipt emailed by Stripe"
confirmation at the top of the Extension screen, gated on `status.active`.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:168-178`; `api/onboarding.py:195-199,228-247`

---

## Transition 5 — Stripe Checkout (cancel) → Info?cancelled=1

**Operator's likely question at this moment:** "Did I just get charged for bailing?"
**Does the UI/email answer it?** Yes
**Friction severity:** Polish
**Current behavior:** Cancel returns to `/info?cancelled=1` (`onboarding.py:200`); `Info.tsx:22-24,68-72`
reads the flag and shows an amber banner: "Checkout was cancelled. No charge was made — you can try
again below." Clear and reassuring. Note (non-operator-facing): each Checkout attempt creates a new
pending Tenant row + token; abandoned attempts leave orphans, but the 409 "account already exists"
guard only matches `active==True` tenants (`onboarding.py:171-177`), so re-entry is not wedged.
**Recommended fix:** None required for the operator. Housekeeping: a sweep for orphaned
`pending_payment` tenants would keep the table clean.
**Files involved:** `web/onboarding/src/screens/Info.tsx:22-24,68-72`; `api/onboarding.py:171-177,200`

---

## Transition 6 — Extension screen: install button → Chrome Web Store

**Operator's likely question at this moment:** "I clicked Install and the store page doesn't exist —
is this a scam?"
**Does the UI/email answer it?** No
**Friction severity:** Blocker *(conditional on external state — verify)*
**Current behavior:** `CHROME_STORE_URL` is a concrete listing URL
(`Extension.tsx:17`, `…/detail/solar-operator-sync/ocohbimolfpnkjcjhiodopjjlhclinpl`). The
"still pending publication" warning is gated on `storeUnpublished = CHROME_STORE_URL.startsWith("#")`
(`Extension.tsx:164,189-194`) — which is **false** for the current URL, so the warning can never
render. CLAUDE.md states the extension is "v1.0.1 **pending Chrome Store push**." If the listing is
not actually live, every operator clicks into a 404 with no warning, and the entire onboarding (which
is gated on capture) hard-stalls.
**Recommended fix:** Confirm the listing is published. If it is not, re-point the guard to the real
publication state (or disable the button with the pending notice) so operators aren't sent to a dead
page.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:17,164,180-194`; `CLAUDE.md` (extension status)

---

## Transition 7 — Extension screen: activation code copy → extension options page (out of band)

**Operator's likely question at this moment:** "I pasted the code into the extension — did it take?
Is it linked to my account now?"
**Does the UI/email answer it?** No
**Friction severity:** High
**Current behavior:** The operator copies the activation code (`Extension.tsx:77-86,208-221`), then
leaves the flow to open the extension's Options page, paste, and Save — entirely **out of band**. The
onboarding screen gets no confirmation the paste/save succeeded; the only eventual signal is a capture
landing. Compounding it: the code box shows "Loading…" until `/status` returns `activation_code`
(`Extension.tsx:47-75,209-211`), and after 20 retries (~60s) it stops retrying silently, so a
webhook-lagged operator may have nothing to copy. There is no "Test connection" / verify affordance
(this is exactly MEGA-VECTOR V3).
**Recommended fix:** Add a "Test connection" button that confirms the activation code is bound and the
extension is reachable, with explicit success/fail feedback in-flow. Show an error (not perpetual
"Loading…") if the code never resolves.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:47-86,197-221`; `extension/options/options.html`; `api/onboarding.py:228-247`

---

## Transition 8 — Extension screen: GMP login button → GMP portal

**Operator's likely question at this moment:** "I logged into GMP — is the extension actually seeing
it? How long should this take?"
**Does the UI/email answer it?** Partial
**Friction severity:** Medium
**Current behavior:** The button opens `greenmountainpower.com/account/` in a new tab
(`Extension.tsx:20,249-256`). Back on the onboarding tab a status pill says "We're waiting for your
first GMP capture… Checking every few seconds" (`Extension.tsx:263-283`). It confirms *we* are
polling, but never confirms the *extension* is installed/active or watching the GMP tab — so a
mis-installed extension looks identical to "just waiting."
**Recommended fix:** Have the extension post a heartbeat the onboarding screen can read, so the pill
can distinguish "extension active, waiting for GMP" from "extension not detected yet."
**Files involved:** `web/onboarding/src/screens/Extension.tsx:20,249-290`; `api/onboarding.py:252-269`

---

## Transition 9 — Extension screen: capture detected → auto-advance to Clients

**Operator's likely question at this moment:** "Wait — what just happened, did it work, why did the
screen change?"
**Does the UI/email answer it?** Yes
**Friction severity:** Polish
**Current behavior:** On a successful ping, the pill flips to "Capture received — taking you to the
next step…" and `advance()` calls `markExtensionInstalled` then navigates to `/clients`
(`Extension.tsx:102-123,272-276`). The announcement exists but the auto-advance can yank an operator
mid-read on the very next 3s tick.
**Recommended fix:** Hold a brief (~1.5s) "Captured ✓" confirmation before navigating, or require a
single "Continue" tap so the success registers.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:88-147,263-290`

---

## Transition 10 — Extension screen: 30s without capture → troubleshooting modal

**Operator's likely question at this moment:** "Nothing's happening — what am I doing wrong?"
**Does the UI/email answer it?** Partial
**Friction severity:** Medium
**Current behavior:** After `HELP_THRESHOLD=10` waiting ticks (~30s), a pulsing "Having trouble?"
button appears (`Extension.tsx:29,166,292-302`) opening a modal with a 3-item checklist and a
support mailto (`Extension.tsx:326-377`). Good, calm escalation. Gap: the checklist is the *only*
diagnostic — there's still no in-flow "test connection," so the operator self-diagnoses by re-reading
steps rather than getting a definitive answer.
**Recommended fix:** Add an active connection test inside the modal (ties to T7) so "having trouble"
yields a concrete pass/fail rather than another checklist.
**Files involved:** `web/onboarding/src/screens/Extension.tsx:22-29,135-166,292-377`

---

## Transition 11 — Clients screen: add client, add array, autopop toggle

**Operator's likely question at this moment:** "If I turn on auto-populate, *whose* GMP login goes
here — mine, or each client's? And where did the array fields go?"
**Does the UI/email answer it?** Partial
**Friction severity:** High
**Current behavior:** Toggling auto-populate hides the manual array entry and asks for a "GMP login
(email or username)" (`Clients.tsx:240-259`). Conceptually this is the **client's** GMP credential,
but Screen 3's capture came from whatever GMP session the **tenant** was logged into — two different
mental models presented without bridging copy. A stamping agent often does not hold each operator's
GMP credentials. Separately, the dashboard has the V4 spreadsheet importer but **onboarding does
not** — the actual buyer (a consultant with a roster) must hand-type every client/array here, which
is the 2-hour grind V4 was meant to kill. The Starlake sub-meter warning is well done
(`Clients.tsx:99-103,187-198`).
**Recommended fix:** Add bridging copy clarifying which GMP login auto-populate expects and how it
relates to the Screen-3 capture. Bring the V4 spreadsheet importer into the onboarding Clients step.
**Files involved:** `web/onboarding/src/screens/Clients.tsx:99-118,239-315`; `web/app/src/components/ImportSpreadsheetModal.tsx`

---

## Transition 12 — Clients screen: finish → Done

**Operator's likely question at this moment:** "I just entered 10 arrays — is my billing now right
for all of them?"
**Does the UI/email answer it?** No
**Friction severity:** High
**Current behavior:** `handleFinish` submits clients, then completes onboarding
(`Clients.tsx:120-174`). Server-side, `_reconcile_subscription_quantity` trues up the Stripe quantity
to the real array count — **best-effort and silent to the operator**; on failure only Ford gets an
internal alert (`onboarding.py:289-347,414-417`). The operator leaves believing billing matches their
array count when it may not. Money-path opacity.
**Recommended fix:** Surface the reconciled quantity on the Done screen ("You're set up for N arrays
at $45/array/month"). If reconciliation fails, tell the operator we're finalizing billing and will
confirm, rather than showing nothing.
**Files involved:** `web/onboarding/src/screens/Clients.tsx:120-174`; `api/onboarding.py:289-347,352-419`

---

## Transition 13 — Done screen → Dashboard /accounts (session minted)

**Operator's likely question at this moment:** "Will I have to log in again, or am I really signed in?"
**Does the UI/email answer it?** Yes (mostly)
**Friction severity:** Medium
**Current behavior:** `completeOnboarding` mints a session and `Clients.tsx:162-164` stores it as
`so_session`; since onboarding and dashboard share the origin, the Done CTA to
`https://solaroperator.org/accounts/` lands signed in (`Done.tsx:8,75-82`). Done says so explicitly.
Edge case: if `session_token` came back null (mint failure), the operator clicks "Go to dashboard"
and hits the Login screen — directly contradicting "You're signed in." Also three emails fire at
`/complete` (welcome + magic-link + sample, `onboarding.py:441-468`) and can arrive out of order /
race in the inbox (JOURNEY finding 12).
**Recommended fix:** Fall back gracefully if no session token (explain a sign-in link was emailed
instead of claiming "signed in"). Consider consolidating or sequencing the three completion emails.
**Files involved:** `web/onboarding/src/screens/Done.tsx`; `web/onboarding/src/screens/Clients.tsx:156-165`; `api/onboarding.py:424-472`

---

## Transition 14 — Dashboard: Account → Clients → Reports tab transitions

**Operator's likely question at this moment:** "Where do I find X?"
**Does the UI/email answer it?** Yes
**Friction severity:** Polish
**Current behavior:** `DashboardLayout` loads the account once and shares it via outlet context; the
Clients tab loads its own data so it doesn't block on the account fetch (`DashboardLayout.tsx:32-78`).
Tabs are clearly labeled Account / Clients / Automatic Reports (`DashboardLayout.tsx:20-24`). Account
and Reports both render a graceful "Couldn't load — refresh" state (`AccountTab.tsx:9-21`,
`ReportsTab.tsx:9-21`). Smooth; no real "wait, what?"
**Recommended fix:** None. Minor: the route is `/reports` but the label is "Automatic Reports" —
harmless but a small naming asymmetry.
**Files involved:** `web/app/src/screens/DashboardLayout.tsx:20-78`; `web/app/src/screens/AccountTab.tsx`; `web/app/src/screens/ReportsTab.tsx`

---

## Transition 15 — Reports tab: change frequency, toggle cc, save customization, preview modal

**Operator's likely question at this moment:** "I just switched to Quarterly — *when* does my next
report actually go out?"
**Does the UI/email answer it?** Partial
**Friction severity:** Medium
**Current behavior:** Frequency change is optimistic with a toast "Reports now send quarterly"
(`ReportsCard.tsx:42-60`) but gives **no next-send date** — switching to quarterly mid-quarter can
mean the next report is ~3 months out with no indication (JOURNEY finding 5). Frequency is **editable
in two places** — the ReportsCard segmented control and the AccountSummaryCard dropdown
(`AccountSummaryCard.tsx:113-126`) — which can confuse/desync. The cc toggle, email customization, and
preview modal are strong (V2): the preview shows From/To/Subject and renders the tenant's own
template server-side (`EmailCustomizationCard.tsx:83-95,258-314`), and the custom-domain fallback is
explained (`…:139-142`). Minor: the preview's hardcoded sample client is the live pilot's real name,
"Bruce Genereaux" (`EmailCustomizationCard.tsx:309`).
**Recommended fix:** Show the computed next-send date on frequency change. Consolidate to one
frequency control. Swap the hardcoded "Bruce Genereaux" sample for a neutral demo name.
**Files involved:** `web/app/src/components/ReportsCard.tsx:42-84,148-153`; `web/app/src/components/AccountSummaryCard.tsx:113-126`; `web/app/src/components/EmailCustomizationCard.tsx:83-95,258-314`

---

## Transition 16 — Clients tab: import spreadsheet → preview → commit

**Operator's likely question at this moment:** "If I import this roster, will it duplicate clients I
already have? Did the AI read it right?"
**Does the UI/email answer it?** Partial
**Friction severity:** Medium
**Current behavior:** The modal flows upload → parsing → an editable preview table → commit, with
"nothing is saved" until commit and a clear post-commit toast (`ImportSpreadsheetModal.tsx:59-135,
218-311`). Two gaps: (1) no warning about collisions with **existing** clients/arrays — the operator
can't tell from the preview whether a row will create a duplicate or merge; (2) `IngestPreview.source`
("llm" vs "heuristic") is returned by the API but never shown, so the operator has no signal about how
reliable the parse is.
**Recommended fix:** Flag rows that match existing clients/arrays in the preview, and state whether
parsing used the LLM or the heuristic fallback so the operator knows how carefully to review.
**Files involved:** `web/app/src/components/ImportSpreadsheetModal.tsx:59-135,218-311`; `web/app/src/lib/api.ts:373-425`; `api/ingest.py`

---

## Transition 17 — Magic-link request → email → click link → session set → dashboard

**Operator's likely question at this moment:** "I clicked my sign-in link and it just dumped me on
the login page — why? Did it expire?"
**Does the UI/email answer it?** No
**Friction severity:** High
**Current behavior:** Login → "Check your inbox," with a correct 15-min/one-time note
(`Login.tsx:51-74`). The email carries a button + raw link and states the expiry
(`account.py:269-279`). On click, `App.tsx` exchanges the token via `/v1/auth/verify`. **The failure
path is swallowed:** `App.tsx:55-62` catches the verify error and silently "falls through to whatever
session we have," then strips the token from the URL — so an expired/used link drops the operator on a
bare login screen with **no explanation**, even though the API returned the exact reason ("Sign-in
link expired — request a new one" / "This sign-in link was already used", `account.py:306-310`).
Separately, `/v1/auth/request` always returns `delivered: true` (anti-enumeration,
`account.py:289-294`), so someone who isn't a customer sees "Check your inbox" and waits for an email
that never comes.
**Recommended fix:** Surface the verify failure reason on the login screen ("Your link expired —
request a new one"). For unknown emails, keep the privacy-preserving response but soften the copy
("If that email is registered, a link is on its way").
**Files involved:** `web/app/src/App.tsx:46-77`; `web/app/src/screens/Login.tsx:24-74`; `api/account.py:235-316`

---

## Transition 18 — Every email send: notification? read receipts? bounce handling?

**Operator's likely question at this moment:** "Did my client actually *receive* this report? That's
the whole reason I'm paying you instead of doing it myself."
**Does the UI/email answer it?** No
**Friction severity:** Blocker
**Current behavior:** Sends are **fire-and-forget**. `_send_via_resend` returns a bool but:
- **False-positive success:** "Send a report now" toasts "Report is on its way to your clients" on any
  HTTP 200 (`ReportsCard.tsx:86-99`), and the frontend never inspects the result. `deliver_for_tenant`
  can return `{"ok": false, "reason": "no recipient email on file"}` or `email_sent: false` and still
  produce a 200 (`account.py:547-555`; `delivery.py:83-99,156-173`) — so the operator is told it sent
  when it didn't.
- **No bounce / delivery webhook anywhere.** There is no Resend event handler (only the Stripe webhook
  exists). Bounces, complaints, and delivered events are invisible.
- **No notification on scheduled sends.** Unless `cc_on_reports` is on, the operator gets *zero* signal
  that a quarterly report went out — no proof of delivery, the exact reassurance the human consultant
  sold (MEGA-VECTOR North Star).
- **Silent "send as me" downgrade.** If the tenant's custom From domain is unverified, the send retries
  from the platform address with the tenant as Reply-To (`notify.py:79-109`) — correct for delivery,
  but the operator is never told their report did **not** go out under their own name.
**Recommended fix:** (a) Make manual send report partial/failed results truthfully. (b) Add a Resend
webhook for bounces/complaints/delivered and a per-client "last delivered / bounced" health indicator
on the dashboard. (c) Notify the operator (or at least record) when a custom-From send was downgraded.
**Files involved:** `web/app/src/components/ReportsCard.tsx:86-99`; `api/account.py:547-555`; `api/delivery.py:83-173`; `api/notify.py:28-109`; (no Resend webhook handler exists)

---

## Transition 19 — Sign out flow

**Operator's likely question at this moment:** "Am I fully signed out?"
**Does the UI/email answer it?** Yes
**Friction severity:** Polish
**Current behavior:** TopNav "Sign out" → `onSignOut` clears the local session and navigates to
`/login` (`TopNav.tsx:21-23`; `App.tsx:80-84`). Clean and immediate. Minor: it's a one-click action
with no confirmation, and it clears only the client-side `so_session` — the server-side session token
is not invalidated, so it remains valid (up to its 30-day TTL) if it leaked elsewhere.
**Recommended fix:** None required for UX. Security hardening: optionally invalidate the session
server-side on sign-out.
**Files involved:** `web/app/src/components/TopNav.tsx:8-27`; `web/app/src/App.tsx:80-84`; `web/app/src/lib/api.ts:22-24`

---

## Transition 20 — Stripe webhook lag (paid but tenant not yet active) — what does the Extension screen show?

**Operator's likely question at this moment:** "I just paid — why does it say 'Loading…' forever, and
why is it now telling me to *pay again*?"
**Does the UI/email answer it?** No
**Friction severity:** Blocker
**Current behavior:** Activation depends on the async `checkout.session.completed` webhook
(`stripe_webhook.py:54-92`). Until it lands, on the Extension screen:
- The activation code polls `/status` 20× over ~60s then **stops, leaving "Loading…" with no error**
  (`Extension.tsx:47-75`).
- `extension-ping` keeps polling but no capture is possible on an inactive tenant.
- Clicking the manual escape "I've installed it" calls `extension-installed`, which for a
  `pending_payment` tenant returns **HTTP 402 "Complete payment before installing the extension"**
  (`onboarding.py:272-284`) — i.e., we tell a customer who **already paid** to pay again.
- The `session_id` is present in the success URL (`onboarding.py:195-199`) but the onboarding flow
  **never uses it to self-heal** — there's no fallback lookup to confirm the Checkout directly
  (JOURNEY finding 1). The operator can end up "paid but inactive" with no recovery affordance.
**Recommended fix:** Add a "Confirming your payment…" state on arrival; use the `session_id` to verify
the Checkout server-side and self-activate if the webhook is late; never 402 a tenant we just
redirected from Checkout — instead show "Finalizing your payment, one moment."
**Files involved:** `web/onboarding/src/screens/Extension.tsx:44-75,149-162`; `api/onboarding.py:195-199,272-284`; `api/stripe_webhook.py:54-92`

---

*End of F1 — Transitions & State Changes.*
