# Ship a new extension build to Bruce (build → GitHub release → email link)

Recurring task: Ford asks to "email my dad a link to the newest extension."
Bruce (bruce.genereaux@gmail.com) is a REAL live-prod array owner — handle with
deletion-safety care and NEVER email him a dead link or a build missing the fix
he actually needs. There are 8+ `scripts/email_bruce_extension_v*_link.py` in the
repo — this is a CLASS of task, reproduce the latest script with modifications.

## The end-to-end pipeline (do all of it, verify each step)

1. **Find the TRUE current version.** Read `extension/manifest.json` `version`.
   This is the SOURCE version — it is NOT necessarily published.
2. **Check what's actually PUBLISHED.** `gh release list --limit 15` in
   `/root/solar-operator`. Releases are tagged `ext-vX.Y.Z` with asset
   `energyagent-extension-vX.Y.Z.zip`.
   - VERSION-GAP TRAP (hit Jun'26): the manifest was at v1.9.46 but the newest
     published release was v1.9.45 — AND v1.9.46 was exactly the build with the
     fix Bruce needed (VEC/SmartHub NEPOOL generation capture). Emailing the
     v1.9.45 link would have handed him an extension that can't do the thing.
     ALWAYS reconcile manifest-version vs newest-release; if the manifest is
     ahead, BUILD + PUBLISH the manifest version first, don't email the stale one.
3. **Build the zip.** `bash scripts/build_extension_zip.sh` — reads the manifest
   version, writes `energyagent-extension-vX.Y.Z.zip` to Ford's Desktop archive +
   both Desktop roots (`/mnt/c/Users/fordg/Desktop/`, OneDrive). 110KB is normal.
4. **VERIFY THE FIX IS IN THE ZIP** (my notes: past builds shipped WITHOUT the
   fix). Unzip to /tmp, `grep` the changed lines + confirm `manifest.json`
   version. E.g. for the NEPOOL capture: `grep -c paired smarthub_content.js`,
   `grep SMARTHUB_METER_GEN_CAPTURED smarthub_content.js`, and
   `grep -c utility-meter-capture background.js` should all be non-zero.
5. **Publish the GitHub release** with the asset. The email URL pattern is
   EXACT — the asset filename in the release must be
   `energyagent-extension-vX.Y.Z.zip`:
   `gh release create ext-vX.Y.Z "<zip-path>#energyagent-extension-vX.Y.Z.zip" --title "EnergyAgent Extension vX.Y.Z" --notes "..."`
   (The `#name` suffix on the path forces the asset's display name regardless of
   the local filename.)
6. **VERIFY THE DOWNLOAD URL RESOLVES** before emailing — never send a guessed
   or unverified link:
   `curl -sL -m 30 -o /dev/null -w "%{http_code} %{size_download}\n" <DL>`
   Expect `200` and a byte size matching the built zip.
7. **Write + send the email.** Reproduce the latest
   `scripts/email_bruce_extension_v<NNNN>_link.py` with the new version/URL.
   Pattern: `to=[TO, BCC_FORD]` (Bruce + Ford for delivery proof), Georgia-serif
   HTML + plain-text, green CTA button, the standard "Load unpacked / Remove old
   tile first / pin it" install steps. `from api.notify import _send_via_resend`.
8. **Add the data-trigger step when relevant.** If the build's value is capturing
   a NEW data source (e.g. VEC production), the install alone doesn't trigger it —
   the extension pulls VEC/SmartHub production CLIENT-SIDE by riding the owner's
   session cookie, so the email MUST tell Bruce to LOG INTO the portal
   (vermontelectric.smarthub.coop) once in the same browser after installing,
   or no generation lands. (See the smarthub generation reference for why.)

## Sending pitfalls (Resend)

- **RESEND_API_KEY must be in the env** for the script to actually send (else it
  logs the email instead and returns False). Pull it from Railway:
  `railway variables --service web --json | python3 -c "import sys,json;print(json.load(sys.stdin)['RESEND_API_KEY'])"`.
  Then `export RESEND_API_KEY=...` (clean key is 36 chars, `re_` prefix).
- **latin-1 / box-drawing corruption (hit Jun'26):** scraping the key from the
  human-readable `railway variables` TABLE picks up box-drawing chars (║ U+2551),
  which Resend then chokes on with
  `ResendError: 'latin-1' codec can't encode character '\u2551'`. ALWAYS use the
  `--json` output for the key, never `grep`/`sed` on the table.
- `_send_via_resend(to=...)` accepts a list at runtime (`[to] if isinstance(to,str) else list(to)`)
  even though the type hint says `str` — Pyright will warn; it's fine, prior
  scripts do the same. Success prints `sent`; on failure it sets
  `_send_via_resend._last_error`.
- SECRET HYGIENE: the secret-masker mangles inline `$(cat ...)`/`$TOK` in shell
  lines. Prefer reading the key inside a python script, or `export` it once.
  Flag to Ford whenever the API key transited the session (it's in the transcript).

## Verify, don't assume
Ford validates outreach by experiencing it as the recipient. The deliverable is
a WORKING link to the RIGHT build, sent to Bruce. Confirm: (a) build version ==
manifest, (b) fix is in the zip, (c) release URL returns 200, (d) Resend returned
`sent`. Report each as a fact, not a claim.
