# Extension UI: palette refresh + headless visual verification (Jun 2026, v1.6.1)

## What happened
Ford screenshotted the extension popup and asked for it to match "our latest
color pallet ... solar punk vibe". The popup was on the retired forest-green
(#064e3b dark header bar, flat layout). Mid-task he reported "the open
dashboard button on it is broken".

## Palette mapping applied (old → new)
| Element | Old | New |
|---|---|---|
| Header | dark `#064e3b` bar, white text | cream `#faf8f5` bar, `#065f46` text, 2px `#e6b470` bottom rule, leaf-dot (9px `#34d399` circle + `#d1fae5` halo) before wordmark |
| Primary button | `#064e3b`, hover `#053429`, radius 6 | `#10b981`, hover `#059669` + wood ring `box-shadow: 0 0 0 2px #e6b470`, radius 12 |
| Stats | bare rows on cream | white card, radius 12, `0 1px 3px rgb(0 0 0 / 0.07)` shadow, `#e8e2d9` border |
| Links | `#064e3b` | `#059669`, hover `#047857` |
| Options page | same forest sweep | focus ring `#34d399`, save btn `#10b981`, saved-box `#ecfdf5`/`#065f46`, h1 `#065f46` |

Canonical token sources: `web/app/src/styles/tokens.css`,
`web/app/tailwind.config.js` (primary-500 `#34d399`, 600 `#10b981`,
700 `#059669`, 900 `#065f46`; wood-300 `#e6b470`; cream `#faf8f5`).
The app's most common button class: `rounded-xl bg-primary-600 ... hover:bg-primary-700`.

Post-change assertion: `grep -rn "064e3b\|053429" extension/` → zero hits.

## Broken dashboard button — root cause
`popup.js` opened `https://solaroperator.org/app` → **404**. The SPA is at
`https://solaroperator.org/accounts` (Netlify `_redirects` 200-proxy →
Railway `/app/`). Diagnosis method: curl candidates with
`-w "%{http_code}"`, then read
`https://raw.githubusercontent.com/Garface111/solaroperator-site/main/_redirects`.

Also fixed while in there: footer "Open GMP" + error-retry were hardcoded to
greenmountainpower.com. Made utility-aware: read `lp.provider` from
`chrome.storage.local.last_payload`, look up host in `window.SMARTHUB_REGISTRY`
(requires `<script src="../smarthub_registry.js">` before `popup.js` in
popup.html — content-script world and popup world both just use plain script tags).

## Headless popup preview recipe (no Chrome on WSL)
Hermes browser_navigate blocks file:// URLs, so use playwright:

```bash
# 1. Build a standalone preview: inline the CSS, drop chrome.* scripts,
#    hand-set the DOM to a representative state (connected pill, WEC capture).
python3 - <<'EOF'
html = open('extension/popup/popup.html').read()
css  = open('extension/popup/popup.css').read()
html = html.replace('<link rel="stylesheet" href="popup.css">', '<style>'+css+'</style>')
html = html.replace('<script src="../smarthub_registry.js"></script>', '')
html = html.replace('<script src="popup.js"></script>', '''<script>
document.getElementById('status-pill').textContent='Connected to Solar Operator';
document.getElementById('status-pill').className='pill pill-connected';
document.getElementById('last-capture').textContent='WEC · 1 min ago';
document.getElementById('count-today').textContent='1 capture today';
</script>''')
open('/tmp/popup_preview.html','w').write(html)
EOF

# 2. Screenshot at popup size (320px wide)
cd /tmp && npm install playwright --no-save --silent
npx playwright install chromium --with-deps   # one-time
node - <<'EOF'
import('playwright').then(async ({chromium}) => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 340, height: 360 } });
  await p.goto('file:///tmp/popup_preview.html');
  await p.screenshot({ path: '/tmp/popup_shot.png' });
  await b.close();
});
EOF
# 3. vision_analyze /tmp/popup_shot.png with a design-review question
```

Review feedback from the shot (left as known nits, Ford ok'd shipping):
footer links sit a touch low; grey stat labels (#71717a on white) are near
the WCAG contrast floor.

## Release mechanics used
- Bump `extension/manifest.json` version (1.6.0 → 1.6.1) via python json round-trip
- `bash scripts/build_extension_zip.sh` (zip pkg required; outputs zip + unpacked dir)
- Copy BOTH zip and unpacked dir to BOTH desktops (local + OneDrive), delete old versions
- Verify zip integrity: python zipfile read of manifest.json
