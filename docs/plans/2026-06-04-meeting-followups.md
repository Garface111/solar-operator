# Bruce Meeting Follow-ups — 2026-06-04

Distilled from 4 pages of handwritten meeting notes (Meeting notes/20260604_*.jpg).
Three agents dispatched on disjoint scopes. Each agent owns a feature branch.

## Agent A — Legal / Copy / Marketing  (branch: agent/legal-copy)
SCOPE — ONLY these files:
- web/onboarding/public/privacy.md
- web/onboarding/public/tos.md
- web/onboarding/src/  (screens that link to/render PP/ToS bullets)
- Marketing site (separate repo Garface111/solaroperator-site, clone to /tmp/marketing-site)

TASKS:
1. Rewrite Privacy Policy in plain English. Strip ALL remaining technical/legal jargon:
   no "JWT", no "PII", no "data controller", no "processor", no "cookies (HTTP-only)",
   no "OAuth scopes". Where a technical concept is necessary, explain in one sentence
   a 60-year-old non-technical solar operator would understand. Bruce's exact words:
   "Revise Privacy Policy so it doesn't have coding".
2. Add 3-5 bulleted summary points at the TOP of the PP and ToS — the operator-facing
   tl;dr ("We never sell your data. You can delete your account anytime. We charge per
   array, see Terms."). Plain language, scannable in 10 seconds.
3. Same plain-English pass on tos.md (ToS).
4. Surface PP link on the onboarding screen prominently (not buried in footer).
5. AUDIT and UPDATE all "GMP" references across the marketing site for utility-name
   consistency (Bruce: "Update all GMP references to all websites"). Verify the term
   "GMP" is spelled out at least once per page ("Green Mountain Power (GMP)") and not
   confused with anything else.

DELIVERABLES:
- Backend branch `agent/legal-copy` with the PP/ToS rewrite + onboarding link wiring
- Marketing site changes pushed to Garface111/solaroperator-site main (Netlify auto-deploys)
- 5-line summary

DO NOT TOUCH: api/, web/app/, scripts/, anything Stripe-related, the dashboard SPA.

---

## Agent B — Bug Sweep  (branch: agent/bugs-sweep)
SCOPE — ONLY these files:
- api/adapters/
- api/account.py  (only the spreadsheet import / NEPOOL-assign endpoints)
- api/models.py  (only if removing bill_timing requires a column change)
- api/writers/   (only if bill-timing removal touches the writer)
- web/app/src/components/ClientCard.tsx and adjacent dashboard cards
  (only the "Latest" labeling)

TASKS:
1. PITTSFIELD CAPTURE BUG: Bruce reports the GMP adapter is failing to pick out
   Pittsfield (one of the locations). Reproduce, locate root cause, fix, add a
   regression test. Likely candidates: an account-name regex, a strict equality
   check on city name, an array-name parsing assumption.

2. SPREADSHEET IMPORT BROKEN: the multi-client onboarding via spreadsheet (the
   /v1/account/nepool/preview + commit endpoints AND the AssignNepoolFromSpreadsheetModal
   flow) is "not working" per Bruce. Reproduce with a test xlsx of his format
   (lift one from C:/Users/fordg/Desktop/Solar Operator/GMCS.xlsx if needed —
   read-only). Fix whatever's broken. Confirm both clients AND arrays import.

3. REMOVE BILL-TIMING UI/MODEL CONCEPT: Bruce: "Prior month issue, should be all
   same month — remove bill timing". The dashboard currently has "bill timing"
   helper copy and array-edit form fields. Remove the user-facing concept entirely.
   The Array.bill_offset_months column can stay in the DB (Bruce's Starlake array
   uses 0 vs others using 1, per CLAUDE.md), but it should not be operator-editable —
   it's an internal adapter knob now. Hide the UI, remove the helper copy.

4. "LATEST" → DATE STAMP: anywhere on the dashboard that says "Latest report" or
   "Latest bill" or "Latest [anything]" should show the actual date instead. Find
   all such labels and replace with "<Date>" or "<Relative>, on <Date>".

5. CAPACITY ANSWER: Bruce asked "Can we accommodate 50 client onboarding? 100?"
   Run scripts/multi-client-stress-test (already exists per commit 830fcd9) at
   50 and 100 client counts. Write the result to docs/capacity-50-100.md with a
   plain-English answer: "Yes, we can handle N clients in M seconds, here's the
   bottleneck, here's where it would break."

DELIVERABLES:
- Branch `agent/bugs-sweep` with all five tasks
- 5-line summary including the capacity numbers

DO NOT TOUCH: Stripe code, payment UI, the onboarding flow (web/onboarding/),
PP/ToS files, marketing site.

---

## Agent C — Onboarding Reflow  (branch: agent/onboarding-reflow)
SCOPE — ONLY these files:
- web/onboarding/  (the entire onboarding SPA — Get Started, Welcome, Steps, Done)
- DO NOT touch web/app/ (dashboard SPA), api/, scripts/, or PP/ToS markdown
  (Agent A owns those).

TASKS — restructure the flow per Bruce's exact words:

Current flow: Get Started → Steps 1-N → Payment → Done → Dashboard
New flow:     Get Started → Welcome (explainer) → DUMMY REPORT → Onboarding Steps → Payment → Tiny Thank-You ("cookie") → Dashboard

1. GET STARTED → animation/explainer (CSS/JS, no video). Three short panels:
   - "I need this because..." — why a solar operator would want SO
   - "I need [N] arrays" — array-count framing
   - Explicitly call out: REQUIRES GOOGLE CHROME (+ Chrome extension link)
   Keep total runtime < 12s with skip/next controls.

2. DUMMY REPORT SCREEN — the FIRST substantive thing the user sees after the
   explainer. Show a pre-rendered example GMCS-style report (use the existing
   sample.xlsx if present in api/onboarding_dist/ or generate a fake one inline
   as HTML). Header: "Here's what a finished quarterly report looks like."
   "Prove it before taking money" is Bruce's literal phrase. The user MUST see
   value before being asked to pay.

3. ONBOARDING STEPS — keep existing steps (account, NEPOOL/spreadsheet import,
   extension install) but reorder so payment is LAST. Currently payment is too
   early per Bruce.

4. POST-PAYMENT — tiny "thank-you / your reward" screen. Bruce: "some cookie for
   the customer by paying". Could be a confetti screen, a unique unlock badge,
   a "welcome to the family" certificate, your call — make it feel like the
   payment unlocked something tangible.

5. Surface plain-English bullets from PP and ToS during onboarding for trust
   (NOTE: Agent A is rewriting privacy.md/tos.md in plain English — assume those
   are clean. Just pull the 3-5 bullet summary that Agent A adds at the top of
   each doc and render them on the welcome screen). If Agent A hasn't finished
   when this task runs, use placeholder bullets and leave a TODO.

DELIVERABLES:
- Branch `agent/onboarding-reflow` with the reflowed SPA
- Build the SPA (vite/whatever) into api/onboarding_dist/ so the new bundle
  is what api/app.py serves (this is how the current setup works)
- 5-line summary noting any place you assumed Agent A's work

DO NOT TOUCH: Stripe webhook code (api/stripe_*.py), dashboard SPA, API endpoints.
You MAY pass URL params / state through the existing onboarding state machine.

---

## Coordination notes
- Stripe payment is being LIVE-tested by Ford on main right now. NONE of these
  agents touch Stripe code. If your task seems to require Stripe code changes,
  stop and add a TODO comment instead.
- Each agent commits to its OWN branch. NO agent merges to main. Ford merges
  after review.
- Each agent reads CLAUDE.md for project standards.
- 5-line summary protocol: (1) files touched (2) verification result
  (3) deviations from this plan (4) anything for the next phase (5) confidence 1-10
