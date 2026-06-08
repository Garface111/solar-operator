# Ford TODO — picked up next session

## Future market expansion: any REC-bearing generation
**Noted:** Jun 8 2026.

Solar Operator is built around solar arrays today, but the underlying
mechanic — track generation, mint RECs, sell them quarterly into the
NEPOOL-GIS market via a pixel-matched workbook — applies to **any**
generation type that earns RECs sold to the state / ISO-NE:

- **Wind** (residential turbines, small commercial)
- **Geothermal** (heat-pump systems where qualifying)
- **Small hydro**
- **Anaerobic digestion / biomass** (where state Class I / II rules cover it)
- **Fuel cells** running on renewable inputs

Implications when we get to this:
- `Array` model is already generic enough — the field is `nepool_gis_id` not
  "solar_id", and `mwh` is the universal unit. Mostly a labeling +
  utility-adapter exercise, not a schema rewrite.
- The Chrome extension currently scrapes utility portals for billing
  energy. Wind / geothermal would likely need a different ingest path
  (production-meter API, manufacturer dashboard, hand-entered MWh).
- Pricing model holds: $15/array/month is per NEPOOL-GIS asset, agnostic
  to fuel type.
- Branding question: stay "Solar Operator" with a tagline expansion, or
  spin up "Grid Operator" / "REC Operator" parent brand when we add the
  second generation type. Decide LAST, after a real second-type pilot.

This is a "remember when we revisit growth" note — no work to do now.

---

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
