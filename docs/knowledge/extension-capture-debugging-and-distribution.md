# Extension capture: self-diagnosing logs, unit verification, release distribution

The Chrome extension content scripts (solarweb_content.js = Fronius, etc.)
capture LIVE readings from a logged-in portal. When a capture "does nothing" and
the console is blank of our logs, you're debugging blind. Lessons Jun'26.

## Make capture SELF-DIAGNOSING (loud gate-by-gate logs)
The content script returns SILENTLY at each gate (no intent flag / not signed in
/ capture error), so a blank console tells you nothing. Add a LOG() at EVERY
gate so the console narrates where it stopped:
- On load: `content script loaded vX.Y.Z on <host>` — ABSENCE of this line means
  the extension isn't injected on that tab (the #1 cause of "blank console").
- Per poll tick: `tick #N — intent: yes/NO`, `signed in: yes/NO`,
  `captured systems: N — inverters: N`, and a `✓ capture complete` line.
- Stop SWALLOWING capture-flow exceptions — log them (`capture failed: <msg>`).
This turned a silent failure into "tick #1 — intent: NO" → user clicks Connect
in the app → "intent: yes" → it proceeds. The capture pipeline only runs after
an explicit in-app "Connect <vendor>" click sets an intent flag with a ~10-min
TTL; opening the portal first / stale build / not-injected are the usual stalls.

## Units: NEVER assume — verify against ONE live reading
Fronius devwork series turned out to be WATTS, not kW: a Primo 12.5kW inverter
read ~1699 (impossible as 1699 kW; correct as 1699 W = 1.7 kW). One real daytime
capture settled it. Fix: normalize the series watts→kW ONCE
(`p[1] / 1000`) so integrateKwh, peak, and the live point are all kW (then
current_power_w ×1000 back to W downstream). Ask for the live console line EARLY
(per memory) rather than iterating builds blind.

## Live-freshness window must match the source's real cadence
Captured points were 34-35 min old at midday but the LIVE_FRESH_MS guard was 30
min → genuinely-recent readings rejected → card stayed "no live feed" even after
the units fix. Solar.web's chart updates ~every 30 min, so 30 was too tight.
Widened to 60 min. Lesson: size the freshness window to the SOURCE's update
cadence, not an arbitrary "feels live" number.

## Distribute a build to a non-dev (Ford's dad / array owners)
- Build the zip: `bash scripts/build_extension_zip.sh` (reads version from
  extension/manifest.json — BUMP manifest version first; copies to Ford's C:
  Desktop root + Archives, NOT just Archives).
- Publish a GitHub Release as the shareable link (matches existing pattern;
  releases tagged `ext-vX.Y.Z` with the zip as an asset). Repo is PUBLIC
  (Garface111/solar-operator) so the release download URL is openly shareable.
  `gh release create ext-vX.Y.Z "<zip>#<name>.zip" --target $(git rev-parse HEAD)
  --title "..." --notes "..."` (use --target when the tag isn't pushed).
- VERIFY the asset downloads (curl -sL the asset URL → HTTP 200 + byte match vs
  the Desktop zip) before sending — don't trust the create call.
- Email it via the resend-email skill / api.notify._send_via_resend(...,
  product="array_operator") with SIMPLE non-dev install steps (unzip →
  chrome://extensions → Developer mode → Load unpacked → remove old version).
  Confirm delivery (Resend last_event=delivered).
- A GitHub-release zip is load-unpacked (unzip + Developer mode). The
  frictionless path for a non-dev is a Chrome Web Store publish (separate review)
  — offer it, don't assume.
