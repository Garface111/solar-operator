# Reports Tab Functional Polish

## Goal
The Reports tab works but is utilitarian. Now that the dashboard has
the cream/emerald/wood visual language, make Reports actually
delightful: preview cards per quarter, last-generated timestamps,
one-click regenerate, and a tiny in-page workbook preview.

## Scope (own ONLY these)
- EDIT: `web/app/src/screens/ReportsTab.tsx` (yours alone)
- NEW: `web/app/src/components/reports/*.tsx` — quarter preview card,
  workbook thumbnail, regenerate button etc.
- READ-ONLY: `web/app/src/ui/`, tokens.css

## DO NOT TOUCH
- `web/app/src/components/TopNav.tsx` (billing banner)
- `web/app/src/components/ClientsSection.tsx` (welcome-reveal agent)
- `web/app/src/screens/SettingsTab.tsx` (billing cancel link)
- `api/` (use only existing endpoints — if you need a new one, list
  it in the summary instead of adding it)
- `web/onboarding/`, `extension/`

## What "delightful" means
1. **Quarter cards** — one Card per recent quarter (last 6). Each shows:
   - Quarter label (e.g. "Q1 2026")
   - Status (Draft / Ready / Sent — pull from existing model field)
   - Array count covered
   - Last-generated timestamp (relative: "2 hours ago")
   - "Download .xlsx" primary button
   - "Regenerate" secondary button (calls existing endpoint)
2. **Empty state** — if there are no reports yet, warm card explaining
   "Reports auto-generate when arrays have captured data for a full
   quarter. Yours will appear here." with an illustration placeholder.
3. **Regenerating state** — when user clicks Regenerate, show inline
   spinner on that card; on completion, the card refreshes its data
   without a full page reload.
4. **Subtle hover** — cards lift slightly (shadow + 1px translate).
5. Match the rest of the dashboard's warmth — NO modal dialogs for
   anything; everything inline.

## Tasks
1. Read current ReportsTab.tsx to understand the data shape + endpoints.
2. Read tokens.css + onboarding's Card/Button as visual reference.
3. Build the quarter card component.
4. Replace ReportsTab.tsx body with the new layout.
5. Empty state.
6. Loading/regenerating states.
7. Build clean. Commit per task ('reports: <what>'). Do NOT push.
8. 5-line summary including any backend endpoints you wished existed.

## Constraints
- TypeScript strict.
- No new npm deps.
- DO NOT break existing functionality — if you can't figure out an
  endpoint, leave the old code path in fallback.
