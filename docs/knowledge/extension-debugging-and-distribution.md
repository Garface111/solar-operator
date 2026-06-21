# EnergyAgent Chrome extension — debugging, adapters, and distribution

The `/root/solar-operator/extension/` Chrome extension is critical-path (it
captures GMP/VEC/WEC bills + per-vendor inverter telemetry the backend can't pull
without a paid API key). It's NOT auto-deployed — Ford loads a packaged build by
hand. Discover this ref by listing `references/` (SKILL.md body is over the 100k
hard limit).

## 1. Build + version a new extension release
- Bump `extension/manifest.json` "version" (the build script reads it).
- Update any in-code load-log version string so the console confirms the build.
- `bash scripts/build_extension_zip.sh` → zips the CONTENTS of extension/ (manifest
  at zip root) to Ford's Desktop root + Archives subfolder + an unzipped copy for
  Load-unpacked. Per Ford's HARD rule artifacts go to the LOCAL C: Desktop
  (`/mnt/c/Users/fordg/Desktop/`), never OneDrive (the script copies to both;
  C: is the one that matters).
- VERIFY the build, never trust it blind: unzip the produced .zip to /tmp and
  grep for your changed lines + check the manifest version. A build can succeed
  while shipping stale bytes.

## 2. Self-diagnosing content-script logs (the silent-gate fix)
A content script that returns silently at each gate (no intent / not signed in /
capture error) gives the user a BLANK console and no way to tell why. Ford pasted
a console showing only the page's own warnings (Google Maps, apple-meta) and "it
only shows this." Root cause: the capture's `[solar-operator/fronius]` line never
printed because an earlier gate returned silently. FIX pattern (now in
solarweb_content.js): add a loud prefixed `LOG()` at EVERY gate —
- on load: `content script loaded vX.Y.Z on <host>` (absence = NOT injected on
  this tab; the #1 thing a blank console means).
- per poll tick: `intent: yes/NO`, `signed in: yes/NO`, `captured systems: N —
  inverters: N`, log capture-flow errors instead of swallowing them, and a
  `✓ capture complete` confirmation.
This turns "blank console, no idea why" into a narrated trace. Capture runs only
when a recent "Connect <vendor>" click set an INTENT flag in chrome.storage.local
(TTL ~10 min) — so "intent: NO" almost always means the user opened the portal
WITHOUT first clicking Connect in Array Operator (or the click expired).

## 3. Verify data UNITS against a LIVE daytime capture (don't assume kW vs W)
Vendor chart series units are a classic silent bug. Fronius's Solar.web `devwork`
(Total Power) series was ASSUMED to be kW; a live midday capture settled it: a
Primo 12.5kW inverter read ~1699 — impossible as 1699 kW, perfect as 1699 W =
1.7 kW. So the series is in WATTS. FIX = normalize the whole series ÷1000 ONCE at
ingest so daily-kWh integration, peak, and the live point are all kW (then the
downstream `current_power_w = kw*1000` round-trips correctly). LESSON: when an
adapter's units are "verified by plausible daily kWh landing in prod," that's a
WEAK proof — one real daytime per-inverter reading is the authoritative check.
Ford's email even pre-named this as "the ONLY unverified assumption." Add a
diagnostic LOG that prints raw last points (value + age_min) so a single live
capture confirms units AND freshness without a rebuild.

## 4. Freshness windows must match the SOURCE's real cadence
Same capture: points were 34–35 min old at midday, but the "is this live?"
window was 30 min → genuinely-recent readings got rejected and the card stayed
"no live feed" even after the units fix. Solar.web's chart updates ~every 30 min,
so 30 was too tight → widened to 60 min. When a "live" value won't show, check
the freshness gate against the source's actual update cadence before assuming the
data is missing.

## 5. OAuth refresh-token ROTATION — "worked until I reconnected"
SMA (and any rotating-OAuth vendor) hands back a NEW refresh_token on every
refresh grant and INVALIDATES the one just used. The SMA adapter discarded the
new token, so: connect works → first refresh (~1h) works + returns a new token we
threw away → next refresh reuses the dead original → 401 → plant goes DARK until
the owner manually reconnects (which writes a fresh token). The tell that it's
rotation and not creds/outage: RECONNECTING FIXES IT (reconnect only supplies a
new token). FIX (commit dfd3671, mirrors the AlsoEnergy adapter):
- `inverters/sma.py::_get_token` caches `(access, refresh, expires)`, reuses the
  freshest refresh token, writes the rotated one back into `config` IN PLACE, and
  on 401 clears the dead token so the next call falls back to client_credentials.
- `poller._persist_config_if_changed(conn, cfg)` writes the mutated config back to
  the `InverterConnection` row with `flag_modified(conn, "config")` (JSON columns
  do NOT auto-detect nested mutation) so the rotated token survives access-token
  expiry AND a redeploy (in-memory cache alone dies on redeploy → SMA resets to
  the consumed original). Tests: tests/test_sma_token_rotation.py (rotation
  reused+persisted; dead token cleared). The adapter is still "unverified against
  a live SMA account" — logic-correct + unit-tested, but real proof is leaving a
  live SMA connection alone a day or two and confirming it doesn't go dark.

## 6. Distribute a build to a real owner via a GitHub Release link
There's no Chrome Web Store auto-publish; the durable shareable link is a GitHub
Release with the zip as an asset (repo `Garface111/solar-operator`, tag pattern
`ext-vX.Y.Z`). `gh release create ext-vX.Y.Z "<zip>#energyagent-extension-vX.Y.Z.zip"
--target $(git rev-parse HEAD) --title ... --notes ...` (pass `--target` when the
local tag isn't pushed, else gh errors). VERIFY the asset downloads:
`curl -sL -o /dev/null -w "%{http_code} %{size_download}"` the
`/releases/download/<tag>/<file>` URL and match the byte size. Then email the
DOWNLOAD url (not just the tag page) with non-dev install steps (unzip →
chrome://extensions → Developer mode → Load unpacked → remove old build first).
Email = Resend (`notify._send_via_resend(..., product="array_operator")` +
email_skin), since no messaging platform is wired here. Repo is PUBLIC, so the
release link is openly downloadable — fine for sharing, flag it's not gated.
Ford's dad Bruce (bruce.genereaux@gmail.com) is a real owner — handle with the
usual care.
