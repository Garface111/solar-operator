# HANDOFF → frontend agent building the per-inverter fleet cards

**TL;DR:** Liquid-fill design for the inverter cards is done. Integrate it into your real
card component, then THAT goes live. Two reference files sit next to this note.

## Files in this folder
- `INTEGRATION-SPEC.md` — the spec. Drop-in `LiquidFill.tsx` component, the CSS
  (+ Tailwind equivalents), exact "keep this / replace the progress bar with this"
  notes, the night/Sleeping state, and the bug-avoidance rules.
- `HYBRID-liquid-plus-sparkline.html` — live visual target. Open it, use the
  ☀ Daytime / 🌙 Night toggle to see both states.
- `shot-HYBRID-day.png` / `shot-HYBRID-night.png` — static snapshots.

## What it does
A bubbling green fill rises behind your card content on a frosted plate (so text +
sparkline never wash out). Fill height = `currentW / maxW` — the same number your
OUTPUT NOW % already comes from, so **no new data needed for the day state**. The
liquid **replaces the thin horizontal progress bar**.

## The one backend ask 🙏
The cards have a "Sleeping" night state (calm indigo resting pool instead of a scary
empty tank at night). It must trigger on **sun-down AND zero output**, NOT on
`output === 0` alone — otherwise a noon fault that zeroes every inverter gets
mislabeled "Sleeping" and hides a real outage.

→ Can `api/inverter_fleet.py` expose an **`is_daylight`** flag (or sunrise/sunset)
per array? It already has the lat/long; computing it once server-side beats every
card recomputing it. A client-side `SunCalc` fallback is in the spec if that's a pain.

## Three bugs already solved — please don't re-derive (all detailed in the spec)
1. Keep plate opacity ≥ 0.8 — a transparent plate re-introduces see-through text wash.
2. Memoize bubble positions — or they teleport on every 60s poll re-render.
3. `content-visibility: auto` on cards — for 40+ inverter arrays (perf).

## Why I'm not just merging this myself
The real sparkline-card UI isn't committed in this repo yet (only the backend fleet
data in `inverter_fleet.py` is). Building a parallel card from scratch would collide
head-on with your in-flight frontend. So: you own the card, this is the liquid layer
to fold in. Ping me if anything in the spec fights your component structure and I'll
adjust it.
