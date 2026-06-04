# Friction Roadmap

Synthesis of the three friction-comb lenses (F1 Transitions, F2 Dead-Ends, F3
Implicit Knowledge) against the MEGA-VECTOR North Star. Prioritized into three
waves plus a quick-wins lane. READ-ONLY analysis upstream; this doc proposes work,
it does not implement it.

## The Vector

The North Star is **"From a tool to a trusted service — every screen anticipates
what the operator's about to wonder."** Our real competition is the human
consultant who answered the phone, knew the operator by name, and *reassured them
their report went out clean*. All three lenses converge on the same verdict: the
product works, but it goes quiet — or worse, lies — at exactly the moments a buyer
second-guesses ("did my money go through?", "did my client actually receive the
report?", "what is this field and what does it do to my report?"). This roadmap
sequences the fixes so that the loud-where-it-matters reassurance the consultant
sold becomes the thing the software does best. We close the false-positives first
(they actively erode trust), then the dead-ends (they make us look broken), then
the explanation gaps (they offload our engineering vocabulary onto the customer).

## Headline Findings

- **[Blocker] "Send a report now" lies on partial failure.** Any HTTP 200 toasts
  "Report is on its way to your clients," even when delivery returned
  `{"ok": false, "reason": "no recipient email on file"}`. We claim success exactly
  where the consultant gave proof. (F1 T18, F2 State26)
- **[Blocker] No delivery confirmation anywhere — ever.** Sends are fire-and-forget;
  no Resend bounce/delivered webhook exists; scheduled sends give the operator zero
  signal a report went out. This is the single most direct contradiction of the
  value prop. (F1 T18)
- **[Blocker] Lost onboarding token = blank dead screen with no escape.** Extension
  and Clients tell the operator to "restart from the welcome screen" but render no
  link, never start polling, and the only button silently no-ops. Stranded work.
  (F2 State34, F1 T13)
- **[Blocker] Paid-but-inactive tells a paying customer to pay again.** Webhook lag
  → clicking "I've installed it" returns HTTP 402 "Complete payment before installing
  the extension." We have the `session_id` to self-heal and don't use it. (F1 T20)
- **[Blocker/conditional] Chrome Store link may 404 with no warning.** The "pending
  publication" guard only fires when the URL starts with `#`; the URL is now a real
  store path, so the warning can never render. If the listing isn't live, onboarding
  hard-stalls. *Verify external state before triaging.* (F1 T6, F2 State31)
- **[High] Magic-link failure is swallowed.** Expired/used links drop the operator
  on a bare login form with no explanation — the API already returns the exact
  reason. Looks broken. (F1 T17, F2 State20)
- **[High] Pricing math never reconciles on screen.** Onboarding promises
  "$45/array/month" but neither checkout nor the dashboard ever shows the array
  count or dollar figure driving the bill. The operator can't verify their own
  invoice. (F3 Finding 3, F1 T12)
- **[High] Auto-populate is an invisible black box.** No "last synced," no capture
  status, no freshness signal post-onboarding. The operator can't tell "working,
  waiting" from "broken, never captured." (F3 Finding 4)
- **[High] Engineering vocabulary leaked into the UI.** `bill_offset_months`,
  NEPOOL-GIS ID (mislabeled "optional"), and the GMP-login field all appear with no
  what/where/why — and a wrong value silently corrupts a regulator-facing report.
  (F3 Findings 1, 2, 14)
- **[High] Destructive-action asymmetry is a trap.** Deactivating a client is a
  reversible soft-delete; deleting an array is permanent and cascades to bills — the
  safer action is the more prominent one, with no signposting. (F3 Finding 5)

## The "Send Now / Send Confirmation" Theme

**Concrete fix bundle.** This directly contradicts the value prop ("your report
went out clean") and must be treated as one shippable unit.

Findings: F1 T18 (false-positive success; no bounce/delivery webhook; no scheduled-
send notification; silent "send as me" downgrade), F2 State26 (single-boolean path
can't express partial sends), F3 Finding 17 ("Last report sent" is account-wide,
reads as per-everything), F3 Finding 18 (custom-From silently falls back to platform
address, log-only).

The shape of the fix, layered:
1. **Truth on manual send (small, this week):** `/v1/account/send-report` returns
   per-client results; the UI surfaces "Sent to 4 of 5 — 1 failed (no contact
   email)" instead of a blanket success toast.
2. **Delivery health (larger, Wave 2):** add a Resend webhook for
   bounced/complained/delivered; store last-delivered/bounced per client; render a
   per-client delivery indicator.
3. **Truthful "send as me" (Wave 2/3):** surface custom-domain verification status,
   and notify when a send was downgraded to the platform address.

Files: `web/app/src/components/ReportsCard.tsx:86-99`, `api/account.py:547-555`,
`api/delivery.py:83-173,156-164`, `api/notify.py:79-109`, `api/delivery.py:97-99`
(per-client skip), plus a new Resend webhook handler (none exists today).

## The "Session Loss / Stuck" Theme

**Concrete fix bundle.** F1 + F2 both flag the onboarding wizard's reliance on
`sessionStorage` as the single biggest structural dead-end source. Lost token = a
told-to-restart-but-can't dead screen.

Findings: F2 State34 (Blocker — session-lost message with no link, dead primary
button), F1 T13 (mint-failure on Done contradicts "you're signed in"), F2 State29 /
F1 T7 (activation code never loads after webhook lag → permanent "Loading…"), F2
State30 (interrupted checkout can't resume in a fresh tab).

The shape of the fix:
1. **Never leave a silent no-op button.** Render the session-lost message as a real
   "Restart setup" link to `/` (welcome).
2. **Persist the token in `localStorage`, not just `sessionStorage`,** so an
   interrupted checkout / fresh tab recovers the session — resolves the Blocker and
   two Highs at once.
3. **Replace permanent "Loading…"** on the activation code with a real error +
   manual retry after retries exhaust.
4. **Graceful Done fallback** when no session token is minted (explain a sign-in
   link was emailed instead of claiming "signed in").

Files: `web/onboarding/src/screens/Extension.tsx:46-96,149-162,197-222,304-306`,
`web/onboarding/src/screens/Clients.tsx:120-128,329-331`,
`web/onboarding/src/lib/onboarding.ts:6-23`, `web/onboarding/src/screens/Done.tsx`.

## The "Codes + IDs Are Magic Words" Theme

**Concrete fix bundle.** F3 found that NEPOOL-GIS ID, the activation code, the GMP
account/login field, and `bill_offset_months` all appear without explaining what
they are, where they come from, or what they affect — and several silently degrade
the deliverable when wrong. These are mostly helper-text edits (cheap) with
outsized trust payoff.

Findings: F3 Finding 1 (bill offset — naked engineering field; wrong value
misattributes MWh to the wrong month of a regulator-read report), F3 Finding 2
(NEPOOL-GIS ID mislabeled "optional," no sourcing guidance, becomes the sheet
title), F3 Finding 14 (GMP login field — whose login? does it do anything? — copy
varies across three screens), F3 Finding 13 (activation code never says "keep this
secret" — it's a bearer credential), F2 State13 (blank NEPOOL ID → malformed sheet
title `"<Array Name> ()"` with no warning).

The shape of the fix:
1. **Inline helper text** for bill offset ("Most GMP arrays bill the prior month —
   leave at 1. Set 0 only if this array's bill shows the same month it's
   generated.") and NEPOOL-GIS ID (what it is, where to find it, that it becomes the
   sheet title). Soften/drop "(optional)" on the GIS ID.
2. **Standardize the GMP-login explanation** across onboarding Clients, AddClientModal,
   and ClientCard ("the email/username this client uses to sign into GMP; we match
   captures to them — we never log in for them").
3. **One secrecy line** on the activation code ("Treat this like a password"), plus a
   regenerate option.
4. **Inline badge** on arrays missing a NEPOOL-GIS ID ("Reports will ship without a
   GIS ID").

Files: `web/app/src/components/ArrayList.tsx:169-192,472-489`,
`web/onboarding/src/screens/Clients.tsx:247-258,283-293`,
`web/app/src/components/AddClientModal.tsx:123-137`,
`web/app/src/components/ClientCard.tsx:132-152`,
`web/app/src/components/ActivationCodeCard.tsx:12-46`,
`web/onboarding/src/screens/Extension.tsx:197-222`, `api/notify.py:133-148`.

## The "Pricing Math Invisible" Theme

**Concrete fix bundle.** F3 noted the tenant has no idea how many arrays they're
charged for or where any dollar figure comes from; F1 corroborates from the
transition side.

Findings: F3 Finding 3 (dashboard shows "Utility accounts" and "Bills on file" —
the two counts that *don't* drive the bill — but never an array count or dollar
figure), F1 T3 (checkout shows $250 + $45 for one array with no note it'll be
trued-up to the real count), F1 T12 (`_reconcile_subscription_quantity` trues up
billing silently; operator leaves believing billing matches when it may not), F3
Finding 7 ("Utility accounts" / "Bills on file" counts unexplained), F1 T1
(duplicated, inconsistent pricing line on Welcome).

The shape of the fix:
1. **Show "Billable arrays: N · ~$45 × N = $X/mo"** on the Account summary, sourced
   from the same array count that drives Stripe, with a note it updates as
   auto-populate adds arrays.
2. **At checkout, state the trued-up model** ("today's charge covers setup + first
   array; monthly total adjusts to your real array count after setup").
3. **Surface the reconciled quantity on Done** ("You're set up for N arrays at
   $45/array/month"); if reconciliation fails, say we're finalizing rather than
   showing nothing.
4. **Collapse the duplicated Welcome pricing line** into one.

Files: `web/app/src/components/AccountSummaryCard.tsx:89-134`,
`web/onboarding/src/screens/Welcome.tsx:37-44`,
`web/onboarding/src/screens/Info.tsx:34-55`,
`web/onboarding/src/screens/Clients.tsx:120-174`, `api/onboarding.py:119-146,289-347`.

> Note (Ford's rule): pricing decisions come last, after data integrity is proven.
> These fixes change only *visibility* of the existing pricing math — they do not
> touch the price itself. Safe to ship.

## Themes by Surface

- **Marketing site (Netlify, external):** Not in-repo, so its CTA→Welcome handoff is
  unauditable — a coverage gap to close (pull the landing page into the repo). The
  in-repo edge is the Welcome screen's duplicated/inconsistent pricing copy and the
  unexplained array model. (F1 T1, V5 solarpunk is separate brand work.)
- **Onboarding wizard:** Highest density of blockers — session-loss dead-ends,
  paid-but-inactive 402, Chrome Store 404 risk, activation-code permanent "Loading…",
  no payment-received confirmation, no in-flow connection test, magic GIS/GMP fields.
  This is where trust is won or lost in the first ten minutes.
- **Dashboard:** Structurally healthier (most actions toast on failure), but leans on
  5s-auto-dismiss error toasts and "Refresh to try again" with no button. The big
  gaps are *informational*: no billable-array/price figure, no auto-populate
  freshness, destructive-action asymmetry, status-badge jargon ("comped"),
  send_mode/cc interaction, no merge-arrays affordance.
- **Backend / emails:** The fire-and-forget delivery model is the deepest trust gap —
  no per-client results, no Resend bounce/delivered webhook, silent custom-From
  downgrade, account-wide "last sent" that reads as per-client, three completion
  emails racing in the inbox.

## Three-Wave Fix Plan

### WAVE 1 — Trust blockers (ship this week)

1. **Send-now tells the truth.** Inspect the delivery result; report partial/failed
   sends instead of blanket success.
   - Sources: F1 T18, F2 State26
   - Files: `web/app/src/components/ReportsCard.tsx:86-99`, `api/account.py:547-555`,
     `api/delivery.py:83-99` (return per-client results)
   - Effort: S
2. **Session-lost recovery CTA.** Render "Restart setup" link to `/`; never leave a
   silently no-op primary button.
   - Sources: F2 State34, F1 T13
   - Files: `web/onboarding/src/screens/Extension.tsx:89-96,149-162,304-306`,
     `web/onboarding/src/screens/Clients.tsx:120-128,329-331`
   - Effort: S
3. **Persist onboarding token in `localStorage`.** Recovers interrupted checkout /
   fresh-tab sessions; resolves the session Blocker and two Highs structurally.
   - Sources: F2 State30, State34
   - Files: `web/onboarding/src/lib/onboarding.ts:6-23`, `Info.tsx:45`
   - Effort: S
4. **Paid-but-inactive self-heal.** Add a "Confirming your payment…" state; use the
   `session_id` already in the success URL to verify Checkout server-side; never 402
   a tenant we just redirected from Checkout.
   - Sources: F1 T20
   - Files: `web/onboarding/src/screens/Extension.tsx:44-75,149-162`,
     `api/onboarding.py:195-199,272-284`, `api/stripe_webhook.py:54-92`
   - Effort: M
5. **Surface magic-link failure reason.** Carry the verify error to the login screen
   ("Your link expired — request a new one"); the API already returns it.
   - Sources: F1 T17, F2 State20
   - Files: `web/app/src/App.tsx:54-71`, `web/app/src/screens/Login.tsx`,
     `api/account.py:306-310`
   - Effort: S
6. **Activation-code error state.** Replace permanent "Loading…" after retries
   exhaust with a real message + manual retry.
   - Sources: F2 State29, F1 T7
   - Files: `web/onboarding/src/screens/Extension.tsx:46-75,197-222`
   - Effort: S
7. **"Payment received ✓" banner on the Extension screen.** One reassuring line
   gated on `status.active` closes the biggest single onboarding trust gap.
   - Sources: F1 T4
   - Files: `web/onboarding/src/screens/Extension.tsx:168-178`, `api/onboarding.py:228-247`
   - Effort: XS
8. **Verify / fix the Chrome Store guard.** Confirm the listing is live; if not,
   drive the "unpublished" banner off an explicit config flag, not the `#`-prefix
   heuristic.
   - Sources: F1 T6, F2 State31
   - Files: `web/onboarding/src/screens/Extension.tsx:16-17,164,181-195`
   - Effort: XS to verify, S to fix the guard

### WAVE 2 — High-impact UX (ship next week)

1. **Billable-arrays + price on the dashboard.** "Billable arrays: N · ~$45 × N =
   $X/mo," sourced from the Stripe-driving count.
   - Sources: F3 Finding 3, F1 T12
   - Files: `web/app/src/components/AccountSummaryCard.tsx:89-134`
   - Effort: M
2. **Auto-populate freshness.** Per-client (or account-level) "Last GMP capture:
   <date>" / "No captures yet" indicator + manual Refresh on the clients list. Reuse
   the Extension capture-status concept.
   - Sources: F3 Finding 4, F3 Finding 19
   - Files: `web/app/src/components/ClientsSection.tsx:31-47`, `ArrayList.tsx:82-86`,
     `AddClientModal.tsx:132-136`
   - Effort: M
3. **Bill-offset + NEPOOL-GIS helper text.** Inline explanations; soften "(optional)"
   on the GIS ID; warn on arrays missing a GIS ID.
   - Sources: F3 Findings 1, 2; F2 State13
   - Files: `web/app/src/components/ArrayList.tsx:169-192,472-489`,
     `web/onboarding/src/screens/Clients.tsx:283-293`
   - Effort: S
4. **Harden delete-array.** Mirror the client modal's reassurance in reverse: "This
   is permanent and is **not** like deactivating a client"; consider type-to-confirm.
   - Sources: F3 Finding 5
   - Files: `web/app/src/components/ArrayList.tsx:236-266`
   - Effort: S
5. **Merge sub-meters guidance.** Rewrite the onboarding warning into the actual
   steps (open one array → "Link a utility account" → delete duplicates) and/or add a
   real merge affordance. This is the Starlake case the business cares about.
   - Sources: F3 Finding 15
   - Files: `web/onboarding/src/screens/Clients.tsx:187-198`, `ArrayList.tsx:271-346`
   - Effort: S (copy) / L (real merge UI)
6. **Resend delivery webhook + per-client health.** Handle bounced/complained/
   delivered; store and surface last-delivered/bounced per client. (Bundle layer 2.)
   - Sources: F1 T18
   - Files: new Resend webhook handler, `api/delivery.py:156-164`,
     `web/app/src/components/ClientCard.tsx`
   - Effort: L
7. **GMP-login field: one explanation everywhere.** Standardize copy across the three
   screens.
   - Sources: F3 Finding 14
   - Files: `web/onboarding/src/screens/Clients.tsx:247-258`, `AddClientModal.tsx:123-137`,
     `ClientCard.tsx:132-152`
   - Effort: S
8. **"Test connection" affordance (V3).** In-flow pass/fail that the activation code
   is bound and the extension is reachable; wire it into the troubleshooting modal.
   - Sources: F1 T7, F1 T10, F3 Finding 4
   - Files: `web/onboarding/src/screens/Extension.tsx:47-86,292-377`, `api/onboarding.py:228-269`
   - Effort: M
9. **Next-send date on cadence change.** Show "Next automatic report: <date>" and
   whether the change takes effect now or next cycle. Consolidate the two frequency
   controls into one.
   - Sources: F1 T15, F3 Finding 9
   - Files: `web/app/src/components/ReportsCard.tsx:42-60`,
     `AccountSummaryCard.tsx:113-126`, `api/scheduler.py`
   - Effort: M
10. **Client-side fetch timeouts.** Add `AbortController`/timeout to `request()` and
    `ingestPreview()` so a stalled connection surfaces an error instead of a forever
    spinner/disabled button.
    - Sources: F2 cross-cutting, States 1/6/7/29
    - Files: `web/app/src/lib/api.ts:53-76,394-416`
    - Effort: S
11. **Checkout-trued-up note + Done reconciliation.** State the trued-up billing
    model at checkout; show the reconciled array count on Done.
    - Sources: F1 T3, F1 T12
    - Files: `web/onboarding/src/screens/Info.tsx:34-55`, `Done.tsx`, `api/onboarding.py:289-347`
    - Effort: S

### WAVE 3 — Polish (when time allows)

1. **Collapse duplicated Welcome pricing line + add array-model framing.** (F1 T1) —
   `Welcome.tsx:37-44` — XS
2. **Swap hardcoded "Bruce Genereaux" preview sample for a neutral demo name.** (F1
   T15) — `EmailCustomizationCard.tsx:309` — XS
3. **Rename/hide "comped" → "Complimentary"; add per-state badge legend; past_due →
   "Update payment" prompt.** (F3 Finding 6) — `AccountSummaryCard.tsx:16-35` — S
4. **Activation-code secrecy line + regenerate option.** (F3 Finding 13) —
   `ActivationCodeCard.tsx`, `Extension.tsx:197-222` — XS (copy) / S (regenerate)
5. **Explain "Utility accounts" / "Bills on file" counts** (e.g. "needs ~18 for a
   full report"). (F3 Finding 7) — `AccountSummaryCard.tsx:104-133` — XS
6. **Import preview: flag collisions with existing clients/arrays + show parse source
   (llm vs heuristic).** (F1 T16) — `ImportSpreadsheetModal.tsx`, `api/ingest.py` — M
7. **Import: AI-extraction / data-handling disclosure line.** (F3 Finding 10) —
   `ImportSpreadsheetModal.tsx:154-209` — XS
8. **Import: flag blank-operator rows instead of silently bucketing into
   "Unassigned."** (F3 Finding 11) — `ImportSpreadsheetModal.tsx:108-111,305` — S
9. **Merge-tag typo warning + nudge "Preview before saving."** (F3 Finding 12) —
   `EmailCustomizationCard.tsx:163-181`, `api/email_templates.py:23-67` — S
10. **send_mode ↔ cc_on_reports cross-reference copy** ("under To-me, clients and
    their CCs are not emailed"). (F3 Finding 8) — `ReportsCard.tsx:155-168`,
    `EmailCustomizationCard.tsx:184-219` — XS
11. **Per-client "last sent"; relabel account figure "Most recent delivery."** (F3
    Finding 17) — `ClientCard.tsx`, `AccountSummaryCard.tsx:129-133` — S
12. **Custom-From verification status in UI + per-send fallback indicator.** (F3
    Finding 18) — `EmailCustomizationCard.tsx:131-142`, `api/notify.py:79-109` — M
13. **Persistent (dismiss-only) error toasts + "Retry" button on account-fetch
    failure.** (F2 States 18, 19) — `Toast.tsx:28-29`, `AccountTab.tsx`, `ReportsTab.tsx` — S
14. **Session-expired info toast on 401 redirect.** (F2 State17) — `App.tsx:86-94` — XS
15. **"Resend link" button (with cooldown) on the magic-link sent state.** (F2
    State33) — `Login.tsx:50-74` — S
16. **Dashboard "finish setup" checklist when clients/accounts == 0.** (F2 State32) —
    `AccountTab.tsx`, `ClientsSection.tsx` — S
17. **Hold ~1.5s "Captured ✓" before auto-advancing off the Extension screen.** (F1
    T9) — `Extension.tsx:88-147` — XS
18. **Cancel button + timeout during AI parse.** (F2 State7) — `ImportSpreadsheetModal.tsx` — S
19. **Extension heartbeat** so the waiting pill distinguishes "extension active" from
    "not detected." (F1 T8) — `Extension.tsx:249-290` — M
20. **Privacy & data link in dashboard footer/Account tab.** (F3 Finding 20) —
    `DashboardLayout.tsx:74-76` — XS

## Quick Wins

XS effort (under ~30 min each), high leverage — dispatch ASAP, don't wait for a wave:

- **"Payment received ✓" banner** on the Extension screen (F1 T4) — one line, gated on
  `status.active`. Closes the biggest onboarding money-trust gap.
- **Session-lost "Restart setup" link** (F2 State34) — convert dead text into a real
  link to `/`. Turns a Blocker into a non-event.
- **Surface the magic-link failure reason** (F1 T17) — the API already returns it; just
  carry it to the login screen.
- **Collapse the duplicated/inconsistent Welcome pricing line** (F1 T1).
- **Swap the hardcoded "Bruce Genereaux" preview sample** for a neutral demo name (F1
  T15) — leaks the live pilot's real name to every operator.
- **Rename "comped" → "Complimentary"** in the status badge (F3 Finding 6) — stops
  leaking internal billing jargon to paying customers.
- **Bill-offset + NEPOOL-GIS helper text** (F3 Findings 1, 2) — pure copy, prevents
  silent report corruption.
- **Activation-code "treat this like a password" line** (F3 Finding 13).
- **Verify the Chrome Store listing is actually live** (F1 T6) — a 2-minute check that
  determines whether a Blocker exists at all.

## Anti-Adds

Deliberately **not** recommending these, despite their appearing in the findings:

1. **Server-side session invalidation on sign-out** (F1 T19). Security hardening with
   low real-world blast radius (30-day TTL, client-side clear already happens). It
   doesn't serve the trust/anticipation vector and pulls focus from delivery
   confirmation. Defer to a dedicated security pass.
2. **Collapsing cc_on_reports and send_mode into one unified "Who receives reports"
   control** (F3 Finding 8). The *consolidation* is over-engineering and risks
   regressing two working features; the cheap cross-reference helper text (Wave 3 #10)
   captures ~90% of the value. Anti-Goal: "don't add settings nobody will touch" cuts
   both ways — don't rebuild ones that work either.
3. **Redesigning / removing the `[copy]` subject prefix** (F3 Finding 21). Low impact,
   QA-only confusion; a one-line mention in the toggle helper (already folded into Wave
   3 copy) is sufficient. A prefix redesign is churn for no buyer-trust gain.
4. **Orphaned `pending_payment` tenant sweep** (F1 T5). Pure backend housekeeping,
   never operator-facing, and the 409 guard already prevents re-entry from wedging. Not
   a friction item; track it as ops debt, not roadmap.
5. **Skeleton loaders instead of bare spinners** (F2 State2) and **making the empty
   client card itself a clickable CTA** (F2 State9). Both are aesthetic polish on states
   that are already healthy (Low risk). They don't anticipate any operator question —
   pure gold-plating that distracts from the blockers above.

---

*End of ROADMAP — synthesis of F1 / F2 / F3 against MEGA-VECTOR.*
