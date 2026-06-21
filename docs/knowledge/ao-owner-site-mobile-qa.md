# AO owner-site mobile QA (phones, ≤600px)

The array-operator owner site (vanilla JS, `/root/array-operator/public/`).
When Ford says "make mobile flawless" — this is the recipe + the bugs found.
(Reports consolidation, Trends stack, /match gotcha, sandbox DEFAULTS live in
ao-owner-site-ux-reports-and-sandbox.md — this ref is mobile-only.)

## Mobile QA recipe
1. Confirm `<meta name=viewport content="width=device-width,initial-scale=1">` on
   each .html (index/login/onboarding all have it).
2. Playwright at 390×844, device_scale_factor=2, is_mobile=True, has_touch=True.
   Per page: screenshot full_page → vision_analyze, AND measure page overflow:
   `document.documentElement.scrollWidth - clientWidth`. List culprits = elements
   whose `rect.right > clientW` AND whose computed overflow is NOT hidden/auto
   (elements inside an overflow:auto/clip parent are fine — they scroll/clip).
3. Authed app pages (Reports/Trends/Arrays) are tabs in index.html — seed
   `localStorage` key `so_session` with a minted token
   (`mint_session_for_tenant('ten_paulbozuwa01')`), then click `#tabReports` /
   `#tabTrends` / `#tabArrays`. The landing-page "demo" IS the same authed
   components with demo data → a mobile bug there is the same component bug.

## Bugs found Jun'26 (classes of fix)
- **CSS LOAD-ORDER (root cause):** `mobile.css` was linked BEFORE
  `theme-day.css`/`trends.css`, so its overrides lost despite a "loaded last"
  comment. The mobile sheet MUST be the LAST `<link rel=stylesheet>` in
  index.html. Check `curl index.html | grep stylesheet`.
- **Table overflow:** the Trends BY-ARRAY `<table>` pushed the page ~53px
  sideways. Fix in mobile.css @≤600px: `#trendsRoot,#panelTrends{max-width:100%;
  overflow-x:hidden}` + `.tr-tablewrap{max-width:100%}` + `.tr-table{min-width:
  460px}` so it scrolls INSIDE its wrap instead of expanding the page.
- **No-wrap flex plate clipped:** the fleet hero `.fc-plate` ("88% fleet healthy"
  + counts + $/mo, rendered by command-center.js, styled in styles.css) is a
  horizontal flex that clipped the right counts on a phone. Fix @≤600px:
  `.fc-plate{flex-direction:column;align-items:stretch}` (+ shrink the inner
  font sizes). General pattern: a desktop no-wrap flex row → stack on mobile.
- **Screenshot artifact, not a bug:** a `full_page=True` capture can composite a
  MID-ANIMATION frame (looked like a white bar over onboarding's checklist).
  Re-shoot a SETTLED viewport (wait ~2.5s, no full_page) and verify via DOM
  (opacity, computed position) before believing a visual bug.
- mobile.css already had good rules (tab-bar wrap, sb toolbar wrap, tap targets
  ≥42px, `body{overflow-x:hidden}`). The sandbox fleet-tree is an INTENTIONAL
  touch-pannable canvas on mobile — its inner cards extending past the screen is
  the pan surface, not overflow.

## Verify live
Deploy is MANUAL (`python3 scripts/netlify_api_deploy.py`); git push only updates
GitHub. Confirm a CSS/JS fix is live with `curl <file>?cb=$(date +%s) | grep <marker>`.
