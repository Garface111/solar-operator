# Solar Operator — Onboarding Audit
**Auditor:** Carol Auditor, Vermont Solar Consulting  
**Date:** June 5, 2026  
**Account used:** critic+onb-1780717678609@solaroperator.org  
**Browser:** Chromium 1223 (headless via Playwright, Xvfb for Stripe step)  
**Screenshots:** /tmp/audit-screenshots/ (F, H series = successful run)

---

## 1. Walkthrough-Swap Verdict

### PASS — Old modal gone. New tour firing correctly.

**Old WalkthroughOverlay modal:**
- Grepped the accounts JS bundle (`/accounts/assets/index-DEM95BGR.js`) for `WalkthroughOverlay`, `walkthrough-overlay`, and `ModalWalkthrough` → **zero matches**
- DOM inspection on fresh first-run dashboard load → no element with any walkthrough-overlay class
- **VERDICT: GONE ✓**

**New SandboxWalkthrough:**
- Found in JS bundle: `const yn="so:walkthrough:sandbox-v2:done"` — the localStorage key is correct
- Found the walkthrough state machine: `function Qr(t){ return localStorage.getItem(yn)==="true"||t>=3?"done":t>=2?"loop":t===1?"welcome":"done" }`
- Found walkthrough step content: 7 steps anchored to dashboard elements ("Welcome to your dashboard", "Each card is a client", "Arrays live under each client", "Add NEPOOL IDs in one shot", etc.)
- **On fresh first-run dashboard (localStorage key cleared):** Tour fires as a small "1/7" card positioned over the canvas with an arrow pointing at the first client card. Modal title: "Welcome to your dashboard." Buttons: "Skip tour" and "Get started →"
- Dashboard content visible behind the tour card (non-blocking)
- **VERDICT: FIRING CORRECTLY ✓**

**localStorage key check:**
- `localStorage.getItem('so:walkthrough:sandbox-v2:done')` = `null` on fresh load ✓
- Key only exists after tour completion or "skip"

**Screenshot evidence:** H06_dashboard_fresh.png, H07_walkthrough_state_2s.png show the "1/7" tour card firing. No full-screen overlay present.

---

## 2. Per-Screen Vector Trace

| Screen | Origin (what I was wondering as I arrived) | Direction (next action / detours) | Magnitude (sec to clarity) | Sublime (1-5) | Notes |
|--------|-------------------------------------------|-----------------------------------|---------------------------|---------------|-------|
| **Marketing homepage** (solaroperator.org) | Will this make my quarterly Excel reports go away? | "Get started →" → clear. Also "Demo Array A/B" and sample download. Multiple good CTAs. | ~8s | 4 | H1 "Quarterly solar reports, automated." lands correctly. Sample data visible. Two demos and a download let skeptics probe before committing. |
| **Onboarding landing** (/onboarding/) | OK I clicked Get Started — show me the actual output. | Single CTA: "Start my free setup →". But CTA is below the fold on some viewports. | ~12s | 4 | Shows real NEPOOL table immediately. "Sample — not your real data" label is honest. Pricing shown below table: $15/arr/mo · $250 setup · 14-day trial. Table takes ~10s to read; CTA below it. No ambiguity once read. |
| **Welcome** (wizard step 1) | What is this actually going to cost me and what am I agreeing to? | Check ToS checkbox → Continue. Detour: full Privacy & Terms shown inline before checkbox. Long scroll required. | ~25s | 3 | Pricing box (green) front and center — excellent. But full Terms text is displayed inline, not collapsed. Forces a scroll of ~600px to reach the checkbox. This is the slowest step for a time-pressured reader. |
| **Your Info** (wizard step 2) | How much personal data do they need just to try this? | Fill 4 fields: name, email, password, company. Continue. | ~30s | 4 | Clean form. Only required fields are name/email/password; company is labeled optional. Password rule stated inline ("10 chars, one letter, one number"). Preview of next step at bottom is helpful. |
| **Array Count** (wizard step 3 = "Clients") | Will the price surprise me? How much is this actually going to cost? | Quick pick or type count → "Save payment method & start free trial →" | ~15s | 5 | **Best screen in the wizard.** Live pricing calculator shows "Charged today: $0.00", then "After trial — one-time setup: $250" and "Monthly (N arrays × $15): $X/month". Eliminates all pricing anxiety before card entry. "Ballpark is fine" copy is exactly right. |
| **Stripe Checkout** (checkout.stripe.com) | Wait, am I being charged right now? How much? | Fill card → "Save". Detours: Stripe Link button dominates top; "Save my information" checkbox defaults checked, adds required phone number field; button says "Save" not "Start trial". | ~60s | 2 | **Biggest vector bend.** Stripe page carries no price reminder. Button says "Save" instead of "Start free trial". The Stripe Link checkbox defaults to checked, which makes submission fail until unchecked (phone becomes required). Confusing for anyone who doesn't know Stripe Link exists. The "$5 back" Bank cashback badge also creates confusion — $5 is NOT the price. |
| **Install** (wizard step 4 — extension) | Did the trial actually start? What do I do now? | "✓ Trial started — your account is active." → install extension → open utility portal | ~20s | 4 | Confirms trial immediately at top (good). Clear two-button layout for GMP vs VEC. "Waiting for you to open a utility portal..." async indicator is elegant. Chrome-only requirement is noted. "Continue →" escape hatch lets you skip to dashboard. |
| **First-run dashboard** (accounts/clients) | What does my dashboard actually look like? Where do I start? | "1/7" tour card fires: "Welcome to your dashboard / Get started →" | ~5s | 4 | NEW SandboxWalkthrough fires immediately and correctly. It's a small non-blocking card (not a full overlay), so the dashboard is visible behind it. Pre-created "Your first client" placeholder gives immediate visual confirmation the system works. Trial banner ("15 days left") is the first thing visible above the tour — creates mild urgency before the user has had a moment to breathe. |

---

## 3. Overall Sublime Score

**3.5 / 5**

The onboarding has two genuinely excellent screens (array count pricing calc, landing sample report) and two that bend the vector hard (Stripe checkout, Welcome ToS wall of text). The install step is honest and clean. The dashboard lands solidly.

**Single biggest vector deviation:** The Stripe checkout. The user just read "Charged today: $0.00" and clicked "Save payment method & start free trial →". They arrive at Stripe with:
1. No price confirmation in sight
2. "Pay with 🔗 link" dominating the top (looks like a different product)
3. "Save my information for faster checkout" checked by default — if submitted without a phone number, the form silently fails with "Required" errors on two fields
4. Submit button reads "Save" — not "Start my trial" or "Confirm $0 today"

The user has to stop thinking about their reports and start thinking about a payment form that's fighting them. That's the moment the vector collapses.

---

## 4. Top Friction Findings (ranked by sublime cost)

### #1 — CRITICAL: Stripe Link checkbox defaults to checked, breaks form submission
**Severity:** High — blocks payment completion for users who don't know what Stripe Link is  
**What bent the vector:** "Save my information for faster checkout" is checked by default. When the user fills card + name + ZIP and clicks Save, Stripe validates the (hidden) phone number field for Stripe Link and shows "Required | Required" errors. The card looks fully filled. The form appears complete. Nothing explains why it's failing.  
**Fix:** Set Stripe Checkout's `payment_method_types` to exclude Link, OR pre-set `emailStripePassCheckboxChecked: false` in the checkout session to default the checkbox unchecked. Alternatively, add custom UI instruction "If you see Required errors, uncheck 'Save my information'."

### #2 — HIGH: Stripe page shows no price — $0.00 confirmation vanishes at checkout
**Severity:** High — creates doubt at the most trust-sensitive moment  
**What bent the vector:** The array count step clearly showed "Charged today: $0.00". But the Stripe page shows no amount at all (it's a setup intent), the button says "Save" instead of "Start free trial", and a "$5 back" Bank badge floats in the UI, making users wonder if they're being charged $5.  
**Fix:** Add a `metadata` description or use Stripe's `custom_text` to show "Starting your free trial — no charge today" above the submit button. Consider renaming the checkout submit button via Stripe's `submit_type: "book"` or custom button text.

### #3 — MEDIUM: Welcome screen inline ToS requires scrolling before you can continue
**Severity:** Medium — wastes 15-20s for every new signup  
**What bent the vector:** The full "Privacy & Terms — the short version" is displayed fully expanded on the Welcome screen. It's well-written but long (~600px of content). The ToS checkbox that unlocks Continue is at the very bottom. Every new user must scroll through this before they can proceed, even if they don't want to read it.  
**Fix:** Collapse the Terms section by default with a "Read full Terms & Privacy Policy +" expand toggle (the code already has this structure but it appears expanded on first render). Or move the checkbox above the terms block so users can accept and continue without reading the full text first, with terms accessible on demand.

### #4 — MEDIUM: Trial urgency banner fires before the tour completes
**Severity:** Medium — introduces billing anxiety before the user has seen the product  
**What bent the vector:** The dashboard's first visible element (before the 1/7 tour card) is a teal banner: "15 days left in your trial — we'll charge based on your final array count on June 20. Add clients now so your first bill reflects the right amount." This creates temporal pressure when the user has literally just arrived and needs to learn the interface.  
**Fix:** Show the trial banner only after the walkthrough "Done" state, OR after day 7 (halfway point). During the walkthrough steps, suppress or minimize the countdown banner so users can orient without pressure.

### #5 — LOW: Quick picks start at 10 — small operators with 1-9 arrays must hand-type
**Severity:** Low — mild friction for the long tail  
**What bent the vector:** Quick picks are 10, 25, 50, 100, 250, 500. An operator with 6 arrays (common in VT community solar) has to use the custom input. This is a one-time, minor task, but it signals the product was designed for larger operators.  
**Fix:** Add 5 to the quick picks row, or change the sequence to 5, 10, 25, 50, 100, 250.

---

## 5. Sublime Moments (do not break these)

**Array Count pricing calculator** — "Charged today: $0.00" with live breakdown below is genuinely excellent. It's the screen that converts. A skeptical operator sees the full cost structure in one glance: trial ($0), setup ($250, one-time), monthly ($15 × arrays). The "ballpark is fine" copy relieves the anxiety of getting the number exact. I didn't have to think on this screen. Don't touch it.

**Onboarding landing sample report** — Shows the NEPOOL-format output immediately, before asking for anything. That is the right answer to "will this work?" The "Sample — not your real data" label is appropriately honest. The table is dense enough to be real, sparse enough to read. This is what community solar operators actually send clients; seeing it is the close.

**Install screen async wait state** — "Waiting for you to open a utility portal..." with a yellow dot indicator is remarkably clean. No user action required, no code to copy, no polling button. The page is watching. This is how the product works and the screen communicates it without a word of instruction.

**Password note on Your Info** — "Sign in instantly from any browser — no email-link wait. We also email a magic link as a backup for the future." This answers the objection before it forms. A lot of SaaS magic-link flows create a second-channel dependency that operators hate. This line handles it preemptively.

**"✓ Trial started — your account is active."** on the Install screen — Small green pill at the top. The user just handed over a credit card; the first thing they see is confirmation the trial started. Correct priority.

---

## 6. What I'd Tell My Colleague

*"The math part works — they show you exactly what you'll pay before you give them a card, and the sample report answers the question I had before I even started. Somewhere around the payment step it got a little weird: the Stripe form fought me for a minute over some checkbox I didn't understand, and I wasn't sure if I was being charged $5 or $0. Turned out it was $0, but I had to trust the previous screen rather than the payment screen itself, and that's backwards. The extension install is straightforward, and once you're in the dashboard the tour fires right up and shows you what to do first. Honestly I'd try it — the time I spend on quarterly reports is worth more than $90 a month. Just be ready for the payment form to be a little bumpy."*

---

## Appendix: Screenshots Reference

| File | What it shows |
|------|---------------|
| D01_landing.png | Onboarding landing — sample NEPOOL report table |
| D02_welcome.png | Wizard step 1 — pricing, features, Terms, ToS checkbox |
| D03_your_info.png | Wizard step 2 — name/email/password/company form |
| D04_array_count.png | Wizard step 3 — quick picks + live pricing calculator |
| H02_stripe_filled.png | Stripe checkout — card form filled, Link checkbox unchecked |
| H03_after_payment.png | Wizard step 4 — Install screen, trial confirmed |
| H04_post_payment_1.png | Install screen — extension CTA + utility buttons |
| H05_post_payment_2.png | First-run dashboard with 1/7 tour card firing |
| H06_dashboard_fresh.png | Dashboard reload after localStorage cleared — tour fires again |
| H07_walkthrough_state_2s.png | Identical to H06 — confirms tour persists |
