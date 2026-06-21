# Shared account SPA: per-host branding + the dist build/deploy chain

The customer account dashboard is ONE React/Vite SPA (`web/app/`) served to BOTH
products at `/accounts` and `/app/*` on Railway. Because it's shared, anything
hardcoded to one brand (e.g. `<title>NEPOOL Operator — Account</title>`) leaks the
wrong name on the other product's domain (arrayoperator.com showed the NEPOOL name).

## Per-host branding fix (no React rebuild logic needed)
The Netlify 200-proxy preserves the address-bar hostname, so `location.hostname` is
reliable inside the SPA. Brand cosmetic chrome (tab title, etc.) with a tiny inline
script in `web/app/index.html` `<head>`, BEFORE the module script, so it runs
pre-paint (no flash). Give a neutral static fallback `<title>`:
```html
<title>Account · EnergyAgent</title>
<script>(function(){try{
  var h=location.hostname||"";
  var isAO=/(^|\.)arrayoperator\.com$/.test(h)||h.indexOf("array-operator")!==-1;
  document.title=isAO?"Account · Array Operator":"Account · NEPOOL Operator";
}catch(e){}})();</script>
```

## The build → deploy chain (CRITICAL — editing source alone does nothing)
Railway serves the COMMITTED `api/app_dist/` bundle, not `web/app/`. Pipeline:
1. Edit `web/app/index.html` (or src).
2. `cd web/app && npm run build`  → regenerates `web/app/dist/` (runs `tsc` first;
   a TS error fails the build). `node_modules` is present.
3. `bash build_app.sh` from repo root → copies `web/app/dist` → `api/app_dist`.
4. Commit `api/app_dist/*` (the deployed artifact) + `web/app/index.html` (source).
   NOTE: `web/app/dist/` is GITIGNORED — `git add web/app/index.html api/app_dist/...`
   (don't try to add dist; it'll be refused).
5. `git push` → Railway auto-deploys.

## Safety check for the rebuilt bundle
Vite content-hashes asset filenames. For a content-only change to index.html the
asset hashes should be UNCHANGED (deterministic build) — so only `index.html`
diffs. Before committing, confirm every `assets/<hash>.{js,css}` referenced in
`api/app_dist/index.html` actually exists in `api/app_dist/assets/`; a stale ref
white-screens the dashboard. If hashes DID change, you changed app code — commit
the new asset files too.

## Verify live (title is set by JS at runtime)
The served raw HTML shows the NEUTRAL fallback title + the inline script; the
branded title only appears in a real browser. So verify by grepping the served
HTML for the script + absence of the old hardcoded title, on BOTH hosts:
`curl -s https://arrayoperator.com/accounts | grep -c isAO` (expect ≥1) and
`grep -c 'NEPOOL Operator — Account'` (expect 0).
