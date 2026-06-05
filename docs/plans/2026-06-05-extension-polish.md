# Chrome Extension v1.4.0 — Polish

## Goal
The extension works end-to-end (bridge protocol shipped in v1.3.0).
Now make it feel cared-for: a meaningful popup state, capture-landed
toast inside the extension popup, error surfacing.

## Scope (own ONLY these)
- EDIT: `extension/manifest.json` (bump version to 1.4.0)
- EDIT/NEW: `extension/popup.html`, `extension/popup.js`,
  `extension/popup.css`
- EDIT: `extension/background.js` (only to expose state to popup)
- EDIT: `extension/content.js`, `extension/vec_content.js` (only if
  you need to forward a new event — prefer to leave alone)

## DO NOT TOUCH
- `extension/BRIDGE_PROTOCOL.md` (wire protocol is frozen — additive
  only, and only if you bump the protocol section docs)
- `extension/so_bridge.js` (production bridge, leave it alone)
- `api/`, `web/app/`, `web/onboarding/`

## What "cared-for" looks like
1. **Popup default state**: shows
   - Status pill: "Connected to Solar Operator" (green) /
     "Not paired" (amber) / "API offline" (red)
   - Last capture: "GMP · 2 min ago" or "No captures yet"
   - Count today: "3 captures today"
   - Button: "Open dashboard" → opens solaroperator.org/app
2. **On SO_CAPTURE_LANDED**, popup toasts a green "Captured!" line
   that fades after 3s. If popup is open at the time.
3. **Errors surface**: if background.js had a sync error in the last
   5 min, show it inline in the popup with a "Retry" button.
4. **Visual**: match the dashboard cream/emerald aesthetic — cream
   background, emerald accents, sans-serif body, NO blue. Reference
   the redesigned web/app for color hex codes (read
   `web/app/src/styles/tokens.css` for the values).

## Tasks
1. Read current extension/ source to learn the state shape.
2. Read web/app/src/styles/tokens.css for color values (copy them
   into popup.css as raw hex since the extension can't import).
3. Bump manifest.json version → "1.4.0".
4. Rewrite popup.html + popup.css + popup.js.
5. Add minimal state-broadcast from background.js to popup (use
   chrome.runtime messaging — popup queries on open).
6. Test by loading the extension in dev mode (can't run from this
   agent — just verify no obvious syntax errors with a node syntax
   check or eslint if configured).
7. Commit per change ('ext: <what>'). Do NOT push.
8. 5-line summary.

## Build artifact
After the work, zip the extension as
`/mnt/c/Users/fordg/Desktop/Solar Operator/Archives - Extension Builds/solar-operator-extension-v1.4.0.zip`
(create dir if missing). Use `cd extension && zip -r <path> . -x "*.DS_Store" -x "archives/*"`.

## Constraints
- Manifest V3 strict.
- Vanilla JS only (no bundler in extension/).
- DO NOT break the bridge protocol.
