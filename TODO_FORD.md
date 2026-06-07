# Ford TODO — picked up next session

## Put up the sample account page so Ford can view it
**Asked:** Jun 6 2026, late night, end of merge-sweep session.

Ford wants to see the sample/demo account page (the one new buyers
see during onboarding, with DummyReport / sample NEPOOL data) hosted
somewhere viewable as a standalone page, not gated behind signup.

Likely scope:
- Find the sample/demo route (probably under `web/onboarding/` —
  the critic's audit called out the onboarding landing as showing
  "real NEPOOL table immediately, before asking for anything")
- Surface it at a stable public URL — either `/sample` on the
  marketing site or `/onboarding/` already shows it
- Confirm with Ford WHICH page he means: (a) the onboarding landing
  with DummyReport, (b) the "Demo Array A/B" experience, or
  (c) something else entirely (a real sample tenant view)

Ask before building.
