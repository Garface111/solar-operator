# Agent E — utility-agnostic copy sweep

## Background

Solar Operator was originally GMP-only (Green Mountain Power) and a lot of the
frontend copy still says "GMP" or "Green Mountain Power" in places where it
should say something utility-agnostic. We're shipping a second utility (VEC,
Vermont Electric Cooperative) and want to be ready for more.

Ford's exact words: "go through every line of text that mentions GMP and edit
the text so that it better denotes we can do more than just GMP".

## Reframing principles

Apply these in order:

1. **Keep GMP-specific where it is genuinely GMP-specific.** Examples that
   should NOT change:
   - The Chrome extension's "Pin the extension and visit greenmountainpower.com"
     instruction — IF the instruction is for the user to scrape a SPECIFIC GMP
     account. Generalize the *framing* ("visit your utility's portal"),
     keep the example.
   - A page literally documenting "How GMP data is parsed" or a footnote about
     the GMP-specific bill format.
   - Compliance / regulatory copy referencing GMP by name (only relevant where
     GMP-specific).

2. **Generalize aspirational/marketing language.** Examples that SHOULD change:
   - "Automates your GMP quarterly reports" → "Automates your utility quarterly
     reports (GMP, VEC, and more)"
   - "GMP-compatible" → "Works with GMP, VEC, and other Vermont utilities"
   - Headers / page titles / CTAs that just say "GMP" generically.
   - Onboarding strings telling the user what the product does.

3. **When in doubt, list both with extensibility.** "GMP, VEC, and more" or
   "your utility (GMP, VEC, and other Vermont electric utilities)" reads
   honestly and signals expansion without overpromising.

4. **DO NOT invent utilities we don't support.** Currently supported:
   GMP (live), VEC (in development — Agent D is building the adapter on a
   parallel branch). It is OK to list VEC as supported even though the adapter
   isn't merged yet — by the time this copy ships, it will be.

5. **Do not erase GMP entirely.** GMP is the flagship customer and most users
   recognize it. The goal is "and others", not "instead of".

## Scope — change copy in these places

### Dashboard SPA: `web/app/src/`
Walk every `.tsx`, `.ts`, `.html` file. Look for "GMP", "Green Mountain Power",
"green mountain power", and reframe per principles above. Common spots:
- Page headers, navbar labels
- Empty-state copy on Clients/Arrays/Bills tabs
- Onboarding banner / setup checklist text
- Tooltips on form fields about "GMP account number" etc.
- Toast notifications and error messages

### Onboarding SPA: `web/onboarding/src/`
Same treatment. Common spots:
- Welcome screen explainer text
- Step labels ("Connect your GMP account" → "Connect your utility account
  (GMP, VEC, and more)")
- Form labels on the utility-credentials capture step
- Done screen

**Important caveat:** Agent C is building a REFLOWED onboarding (branch
`agent/onboarding-reflow`) that adds new screens GetStarted.tsx, DummyReport.tsx,
ClientSetup.tsx — those are NOT on main and you will NOT see them. That's OK.
Ford will do a 5-min follow-up reframe pass on those screens after Agent C's
branch is merged. Note this in your final summary.

### Marketing site (separate repo Garface111/solaroperator-site)
Clone to /tmp/marketing-site:
```
git clone https://github.com/Garface111/solaroperator-site /tmp/marketing-site
```
Walk every `.html`, `.md` in the root. Same reframing.

IMPORTANT: Agent A (legal-copy) already edited the marketing site to spell out
"Green Mountain Power (GMP)" once per page for clarity. You may LEAVE those
expansions in place where they're factually correct (a paragraph specifically
about GMP). But where the copy is generic ("works with GMP" as a feature
bullet), update to multi-utility framing.

## SCOPE — only touch these areas
- `web/app/src/` (any file, but ONLY copy/text changes — don't touch logic)
- `web/onboarding/src/` on the CURRENT main branch (not Agent C's reflow)
- Marketing site repo

## DO NOT TOUCH
- `extension/` (Agent D owns extension changes)
- `api/` (no backend changes for copy sweep)
- `web/onboarding/public/privacy.md`, `web/onboarding/public/tos.md`
  (Agent A rewrote those on branch agent/legal-copy — keep that work intact)
- Stripe code

## DELIVERABLES
- Branch `agent/utility-agnostic-copy` with backend repo changes
- Marketing site pushed to its main (Netlify auto-deploys)
- 5-line summary: (1) files touched per repo with counts, (2) verification
  (grep "GMP" before/after — show the remaining intentional refs), (3) any
  copy you weren't sure about, (4) explicit note that Agent C's new
  onboarding screens are NOT covered and need a follow-up pass, (5) confidence
  1-10

## Method
Recommend this approach:
1. `rg -n "GMP|Green Mountain Power" web/app/src/ web/onboarding/src/ > /tmp/gmp_refs_app.txt`
2. Walk through every hit, categorize: keep / generalize / list-both
3. Apply edits
4. Re-run `rg -n "GMP|Green Mountain Power"` to confirm only intentional
   keeps remain
5. Repeat for marketing site
6. Build the SPA (`web/app && npm run build`) if dashboard files were touched,
   so the deployed bundle reflects the changes — same convention as
   `api/app_dist/` for the dashboard or `api/onboarding_dist/` for onboarding
