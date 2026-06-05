# Dashboard Visual Redesign — Match Onboarding Aesthetic

## Goal
The dashboard (`web/app/`) currently "looks worse than the onboarding."
Propagate the Solarpunk cream/emerald/wood/kitchen-table aesthetic from
`web/onboarding/` across every dashboard tab so the product feels
visually consistent end-to-end.

## North star
- Warm, kitchen-table, NOT enterprise blue
- Match onboarding's Card / ScreenLayout / color palette / typography
- DO NOT break ClientCard, CaptureCeremony, WalkthroughOverlay LOGIC.
  You can restyle them but their behavior stays identical.

## Scope (own everything under `web/app/`)
- `web/app/src/index.css` — tokens + base styles
- `web/app/src/components/` — shared primitives + restyle existing
- `web/app/src/sections/` (if exists) or wherever Clients/Reports/Settings live
- `web/app/src/App.tsx` / layout shell

## Reference (READ ONLY)
- `web/onboarding/src/` — source of truth for visual language
- `web/onboarding/src/components/Card.tsx`, `ScreenLayout.tsx`, etc.
- `web/onboarding/src/index.css` — Tailwind config / tokens
- `web/onboarding/tailwind.config.*` and `package.json` for tooling

## Tasks

### Task 1 — Audit + tokens
1. Read onboarding source. Extract color palette (cream/emerald/wood,
   exact hex codes), typography (serif headings? sans body? sizes),
   spacing rhythm, border-radius, shadow language, button styles.
2. Write `web/app/src/styles/tokens.css` (or extend the existing
   Tailwind config / index.css) with CSS custom properties:
   `--so-cream`, `--so-emerald-{50..900}`, `--so-wood-{...}`,
   `--so-radius-card`, `--so-shadow-soft`, etc.
3. Make sure `tokens.css` is imported by `index.css` so every component
   gets them.

### Task 2 — Shared primitives in web/app
Port these from onboarding into `web/app/src/components/ui/`:
- `Card.tsx` — the warm-paper card with soft shadow
- `ScreenLayout.tsx` — page shell with the cream background
- `Button.tsx` — primary (emerald), secondary (wood/outline), ghost
- `Chip.tsx` — for tags / status pills
- `SectionTitle.tsx` — serif heading + supporting subtext

If equivalents already exist in web/app, REPLACE their internals to
match onboarding but keep the EXPORT NAME identical (avoid import churn).

### Task 3 — Restyle the dashboard shell
- Top nav / header: warm cream background, serif wordmark, subtle wood
  bottom border
- Tab nav (Clients / Reports / Settings): emerald active underline,
  not enterprise blue
- Page padding/rhythm matches onboarding

### Task 4 — Restyle each tab
For each of Clients, Reports, Settings:
- Wrap content in the new `ScreenLayout` + `Card`
- Replace plain `<button>` with `Button`
- Replace any blue/gray status indicators with emerald/wood/amber
- Headings use the serif type from onboarding
- DO NOT change behavior — just restyle

### Task 5 — Spot-check CaptureCeremony, ClientCard, WalkthroughOverlay
These are HOT components. Touch only their styles (colors, shadows,
spacing, fonts) to match — do NOT touch their logic. Verify they still
work by reading their tests if any.

### Task 6 — Verify
- `npm --prefix web/app run build` cleanly produces a bundle
- Visual smoke: nothing obviously broken (you can't view it, but you
  can grep for unresolved Tailwind classes, raw `#0000ff`-style enterprise
  colors, etc.)

## Constraints
- TypeScript + React + Vite + Tailwind (detect from package.json — match
  the existing toolchain, don't introduce a new one).
- NO new npm dependencies unless absolutely required.
- DO NOT commit yet — leave dirty for orchestrator review.
- DO NOT push.
- After all tasks: emit a 5-line summary as required.
