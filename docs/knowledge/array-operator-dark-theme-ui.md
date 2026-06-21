# Array Operator dark-theme UI: native controls + visual-QA loop

AO (`/root/array-operator/public/`) is a dark-themed vanilla-JS site. Tokens in
`styles.css :root` — `--bg:#0a0e14 --bg2:#0e131c --ink:#eaf0f7 --line:rgba(255,255,255,.08)`,
brand greens `--good:#3fd68a`. Day theme via `html[data-theme="day"]` in `theme-day.css`.

## PITFALL: native <select> dropdown popups render WHITE on the dark theme
Styling a `<select>`'s box (background/border) does NOT style its popup option list —
the browser paints that OS-level widget white by default, which is unreadable on the dark
bg and "doesn't match the aesthetic" (Ford flagged this; he expects native controls themed).
This is SITE-WIDE, not per-component.

Fix (global, in styles.css right after `:root`):
```css
select,option,optgroup,input,textarea{color-scheme:dark}
select option,select optgroup{background:#0e131c;color:#eaf0f7}
```
`color-scheme:dark` flips the native widget dark; the explicit option colors are a
fallback for engines that honor them. Add the day-mode override in theme-day.css so it
flips back to light when `data-theme="day"`:
```css
html[data-theme="day"] select,option,optgroup,input,textarea{color-scheme:light}
html[data-theme="day"] select option,select optgroup{background:#fff;color:var(--ink)}
```
Verify in Playwright: `getComputedStyle(selectEl).colorScheme === 'dark'` and the option's
backgroundColor === rgb(14,19,28). NOTE: the OS-level open popup renders OUTSIDE the page
DOM, so a screenshot can't capture the open list — the computed-style check IS the
verification; final confirmation is the user opening a dropdown on the live site.

## Visual-QA loop (Ford requires this on every UI change)
Playwright screenshot + vision_analyze on EVERY state; fix clipping/overflow before "done".
Serve over the dev_proxy (localhost http), NEVER file://. Inject the session token into
localStorage before loading `#<tab>`. When something looks like a render bug, crop+2x-zoom
the region with PIL and re-run vision_analyze before concluding — what looked like "ghost
text overlap" this session was just the legit hero marketing copy.

## CSS conventions
Append-only, prefixed by feature: `.rb-*` (reports billing), `.trv-<key>-*` (trends views).
Don't edit trends-core.js / trends.js / other agents' view files (TRENDS-VIEWS-CONTRACT.md).
