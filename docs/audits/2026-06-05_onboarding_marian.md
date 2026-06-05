# Solar Operator Onboarding Audit — by "Marian Thatcher"

*Conducted June 5, 2026 by a skeptical Vermont solar consultant with 4 arrays, 3 LLC clients, and 25 minutes between calls.*

---

## Executive summary

Would I pay $250 setup + $15/array/month for this? Yes, probably — and I came in expecting $45/array/month based on what Bruce told me, so the actual price was a pleasant shock. At four arrays that's $60/month against $540/quarter I'm already billing clients, and the 6 hours I'm currently burning every quarter is genuinely recouped by the first run. The math works. But I would not sign up today, for one specific reason: the contract email the system sends me when my trial ends says "Your 4-day trial just ended" when I was promised 14 days. That is a billing notification that contradicts the advertised terms. Until that's fixed, I can't trust that what I read in the terms is what will happen in practice. Separately, there are two support email addresses scattered across the legal documents — one in the Terms, a different one in the Privacy Policy — and neither says whether anyone reads both. These are the two things I would fix before taking another signup.

---

## Scores by vector

- **Trust: 3/5** — The math on the reports checks out (I read the GMCS writer code and the floor-of-MWh REC formula is correct), Stripe handling is proper, and there's a real 14-day free trial with zero charge today. Trust collapses to 3 because of the "4-day trial" text in the billing notification email (api/notify.py:430) — the one email you send after actually charging my card should not contradict your advertised terms.

- **Transparency: 4/5** — Chrome permissions are explained clearly in plain English in the privacy policy. The extension README is frank about what it captures (JWT grants full GMP access). The onboarding wizard shows exactly $0.00 charged today with a clear breakdown of what posts after trial. Loses one point because the 1-array billing minimum (max(array_count, 1) in stripe_helpers.py:64) is never disclosed to the buyer — it appears only in a triggered email if you hit the zero-array edge case.

- **Anticipation: 3/5** — The CaptureCeremony screen ("watch your clients land here") is genuinely good — it answers "what do I do right now?" with concrete portal buttons. The extension screen has a working "Having trouble?" fallback and a "Continue anyway →" escape hatch. Score drops because the welcome email still describes a manual activation-code-pasting workflow that no longer exists in the wizard (the wizard auto-pairs), and the WalkthroughOverlay opens by assuming "your first utility login is already captured" when most fresh users have captured nothing yet.

- **Reversibility: 4/5** — "Cancel anytime" is real: the dashboard has a cancel-trial endpoint (POST /v1/onboarding/cancel-trial), the ToS is clear that cancellation takes effect at end of billing period, and both the setup fee and monthly fees are explicitly stated as non-refundable. Loses one point because the FAQ says "Your data stays accessible for 30 days after" cancellation but the ToS says "access through the end of the period you already paid for" — these are potentially different time windows (one is 30 days from cancel date, one ends at billing period boundary) and they contradict each other in writing.

- **Buyer's-eye honesty: 3/5** — The landing page example report is real math, not fake numbers. The pricing is exactly what the code charges. The "We never read X" data promises in the Privacy Policy are enforced in the extension manifest (host_permissions are scoped exactly to GMP and VEC). Score drops to 3 because: (1) the 14-day free trial is a significant differentiator that doesn't appear anywhere on the landing page, buried until Step 1 of the wizard; (2) the Chrome Web Store listing describes "every month, around the 20th, we email you your reporting spreadsheet" when the product actually runs quarterly; (3) the landing page says "from $15/array/month" — the word "from" implies tiered pricing that doesn't exist.

---

## Top 10 friction findings (ranked by buyer-trust impact)

**[Severity 5] "4-day trial" in the billing notification email**
After 14 days the scheduler fires `finalize_expired_trials()` and then sends `send_trial_charged_email()`, whose HTML template says "Your 4-day trial just ended and your card was charged $X." (api/notify.py:430). The ToS says 14-day trial. The onboarding wizard says 14-day trial. The backend sets `trial_ends_at = now() + timedelta(days=14)`. This one transposed digit in a billing email is a trust-destroying inconsistency in what should be the clearest contract moment in the entire relationship.
*Fix: change "4-day" to "14-day" at api/notify.py:430.*

**[Severity 5] 1-array billing minimum not disclosed**
When a user's trial ends with zero arrays, `finalize_expired_trials()` charges them for 1 array anyway (`max(array_count, 1)`, stripe_helpers.py:64). The only disclosure is inside `send_add_first_array_email()` (api/notify.py:403): "If you still have zero, we'll charge the 1-array minimum." This minimum appears nowhere in the ToS (section 3 says only "$15 per solar array per month"), nowhere on the landing page, and nowhere in the onboarding wizard pricing breakdown.
*Fix: add one sentence to ToS section 3 and the ClientSetup.tsx pricing card.*

**[Severity 4] Two contact email addresses — no canonical one**
The ToS and the landing page footer say `admin@solaroperator.org`. The Privacy Policy (twice), the Chrome Web Store listing, and the dashboard footer say `support@solaroperator.org`. The Extension help modal (Extension.tsx:519) says `admin@`. If I have a billing dispute, which do I use? Are both monitored? Nothing says so.
*Fix: pick one address, use it everywhere. Until then a confused customer uses the wrong one and gets silence.*

**[Severity 4] 14-day free trial invisible on the landing page**
The landing page hero fine print reads "from $15/array/month · $250 setup · cancel anytime." The 14-day free trial — a substantial differentiator — appears only after clicking "Get started," advancing through an animated 3-panel intro, and reaching Step 1 of the wizard. A buyer comparing tools in a browser tab does not click through a wizard to find the trial. It belongs on the landing page alongside the price.
*Fix: add "14-day free trial — no charge today" to the landing page pricing line.*

**[Severity 4] WalkthroughOverlay assumes capture has happened**
The 7-step tour's opening message (WalkthroughOverlay.tsx:31) reads: "Your first utility login is already captured — we auto-created a client for it and attached its arrays." But fresh users who clicked through the wizard and landed on the dashboard have almost certainly not yet signed into a utility portal. The CaptureCeremony panel handles the zero-capture state correctly ("Waiting for your first capture"). The tour tooltip gives false assurance.
*Fix: make Step 0 conditional on whether captures > 0, with different copy for each branch.*

**[Severity 3] Welcome email describes manual code-pasting that the wizard doesn't require**
The welcome email (api/notify.py:152-163) tells new users: "Paste your activation code — click the Solar Operator icon in Chrome's toolbar, paste the code above, hit Save." But the onboarding wizard (Extension.tsx) auto-pairs via `SO_PAIR` bridge messages — no code-pasting in the wizard. When a user later sets up on a new browser and rereads the welcome email, the flow it describes conflicts with what they learned during onboarding.
*Fix: rewrite the welcome email steps to describe auto-pairing as the primary path, with manual code-entry as the fallback for new devices.*

**[Severity 3] Chrome requirement buried below the fold in the onboarding intro**
The GetStarted.tsx intro has three auto-advancing panels. Panel 3 says "One requirement: Google Chrome." Panels auto-advance every 4 seconds, so a user who clicks "Get started" and pauses may miss it. The main landing page never mentions Chrome. A Firefox or Safari user can enter their name, email, and payment method before hitting the Chrome-specific wall.
*Fix: surface "Chrome required" before payment — at minimum as a note on the ClientSetup.tsx pre-checkout screen. Ideally detect browser and warn early.*

**[Severity 3] Chrome Web Store listing describes monthly emails, product delivers quarterly**
The store listing copy (store_assets/store_listing_copy.md) says "every month, around the 20th, we email you your reporting spreadsheet." The landing page and the ToS say the product generates quarterly NEPOOL-GIS reports. Operators who find the extension listing first arrive with wrong expectations.
*Fix: update store listing to say quarterly, matching the ToS and landing page.*

**[Severity 3] FAQ data-retention language conflicts with ToS**
The landing page FAQ: "Your data stays accessible for 30 days after [cancellation]." The ToS section 4: "access continues until the end of the period you already paid for." If my billing period ends 2 days after I cancel, I lose access in 2 days per the ToS but in 30 days per the FAQ. The FAQ is part of my buying decision.
*Fix: either change the FAQ to match the ToS or add the 30-day guarantee to the ToS explicitly.*

**[Severity 2] Extension version mismatch — store serves 1.0.2, code is 1.0.3**
extension/README.md contains an explicit TODO: "manifest is at 1.0.3 locally, but the Store still serves 1.0.2. The 1.0.3 change... won't reach existing users until we re-submit." New users who sign up today install 1.0.2 and miss the "return to setup" link in the options page — the one affordance that helps them if auto-pairing fails. During the most critical 60 minutes of the funnel.
*Fix: submit 1.0.3 to the Chrome Web Store before the next marketing push.*

---

## Delightful moments

**The pricing math is legible in the wizard.** ClientSetup.tsx shows a live calculation: "Monthly (4 arrays × $15) = $60/month" and "Charged today: $0.00." This is the anti-pattern of every SaaS that hides the math until after the card is entered. I checked the server-side code (stripe_helpers.py, onboarding.py) and the numbers match. That matters enormously to me.

**The ToS plain-English summary.** The Welcome.tsx screen shows privacy and terms as concise bullet points before asking me to agree — not buried legal text requiring a law degree. And then the full legal text IS available via an expand button on the same screen. Exactly how it should work.

**"Having trouble?" appears after 45 seconds, not 5 seconds.** Extension.tsx:37 sets the help affordance to appear only after 45 seconds of silence. This is the right call. Most users who are stuck are stuck because they haven't done the step yet, not because the step is broken. The delay respects that without abandoning people who genuinely need help.

**The CaptureCeremony cascade.** The "watch your clients land here" panel in the dashboard (CaptureCeremony.tsx) is the first WOW moment for a buyer who completes the extension step. Arrays appear with names, chips animate in. This is the moment that converts a trial user. Don't break it.

**"Continue anyway →" throughout.** The onboarding wizard has manual-continue escape hatches on the extension install screen and the extension help modal. A SaaS that doesn't trust its own magic moments enough to force a user through them is rare and welcome.

**The sample report email.** On completing onboarding, the system automatically sends a sample NEPOOL workbook. This is smart. I want to check the format before my trial ends. Receiving the file before I've even set up my first real array removes the "I'll evaluate once I see a real output" postponement.

---

## What I'd tell Bruce on the phone

"The math checks out and the price you quoted me was wrong — it's $15/array/month, not $45. That changes the whole calculation. I'm going to sign up for the trial, but I want to flag something that bothered me: when I traced the trial end-billing email in the code, it says 'Your 4-day trial just ended' when the terms say 14. I'd want to see that fixed before they charge any more consultants, because if my client ever pulled that email as evidence of what they agreed to, we'd have a problem. The rest of it — the extension install flow, the capture, the NEPOOL formatting — all looks solid and I'd feel comfortable sending these to my LLCs. Tell them to fix the email and the two different contact addresses before they run ads."

---

## Appendix: numeric reconciliation table

| Value | Landing page | ToS | Stripe code | Welcome email | Onboarding wizard screen | Trial-end billing email |
|-------|-------------|-----|-------------|--------------|--------------------------|------------------------|
| Setup fee | $250 | $250 | `SETUP_FEE_CENTS=25000` ($250) | not mentioned | $250 (ClientSetup.tsx) | included in `amount_dollars` from Stripe invoice |
| Monthly fee | $15/array/month | $15/array/month | `ARRAY_PRICE_CENTS=1500` ($15) | not mentioned | $15 × count (ClientSetup.tsx) | included in `amount_dollars` |
| Trial length | **not mentioned** | 14 days | `timedelta(days=14)` | not mentioned | 14-day (Welcome.tsx, Info.tsx, ClientSetup.tsx) | **"4-day"** ← ⚠️ MISMATCH |
| Charge today | (implied: $250) | $0 during trial | $0 (setup mode checkout) | not mentioned | **"$0.00"** ✓ | — |
| Array billing minimum | not mentioned | not mentioned | `max(count, 1)` ← undisclosed | not mentioned | not mentioned | not mentioned |
| Trial extension | not mentioned | not mentioned | +3 days if 0 arrays | not mentioned | not mentioned | mentioned only in zero-array triggered email |
| Data post-cancel | "30 days" (FAQ) | "through billing period" ← **CONFLICTS** | — | — | — | — |
| Contact (billing) | admin@ | admin@ | — | "just reply" (no explicit address) | admin@ (help modal) | "just reply" |
| Contact (privacy/data) | — | admin@ | — | — | **support@** (dashboard footer) | — |
| Contact (store listing) | — | — | — | — | — | **support@** |

**Mismatches flagged:** Trial length in billing email (4 vs 14 days), data retention post-cancel (30 days vs billing period), contact email (admin@ vs support@ depending on surface).
