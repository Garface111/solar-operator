# Cut a GitHub extension release + email the download link

When Ford says "email my dad the link to download the latest extension (vNN)",
the version he means is almost always the **manifest version** of the committed
code — NOT necessarily a published GitHub release. These drift apart.

## The trap (observed Jun 2026)

- `extension/manifest.json` was at `1.9.40` and commits existed for v1.9.38/39/40.
- But `gh release list` topped out at `ext-v1.9.22` — releases v1.9.38–40 were
  NEVER tagged/released. The only downloadable asset was the v1.9.22 zip.
- So "send the v40 download link" had NO link to send. Sending the manifest
  version's URL blind would 404; sending v1.9.22 would be 18 versions stale.

ALWAYS reconcile before emailing: compare `manifest.json` version vs.
`gh release list`. If the release is missing, cut it first, then email.

## Procedure (verified working)

1. Confirm version + clean-ish tree:
   `python3 -c "import json;print(json.load(open('extension/manifest.json'))['version'])"`
   `git log --oneline -1`  (tag the release at this HEAD)
2. Build the zip with the repo's own script (reads version from manifest, zips
   the CONTENTS of extension/ so manifest is at zip root, drops copies on both
   Desktop roots):
   `bash scripts/build_extension_zip.sh`
   → `energyagent-extension-vX.Y.Z.zip`
3. Create the release at current HEAD with the zip attached:
   `gh release create ext-vX.Y.Z "/path/energyagent-extension-vX.Y.Z.zip#energyagent-extension-vX.Y.Z.zip" \
      --target "$(git rev-parse HEAD)" --title "EnergyAgent Extension vX.Y.Z" --notes "..."`
4. VERIFY before emailing (mandatory — don't send an unverified link):
   - `gh release view ext-vX.Y.Z --json assets,url` → asset present, nonzero size
   - download URL returns HTTP 200:
     `curl -sIL -o /dev/null -w "%{http_code}\n" \
       https://github.com/Garface111/solar-operator/releases/download/ext-vX.Y.Z/energyagent-extension-vX.Y.Z.zip`
5. THEN email the link via the resend-email skill (direct curl path; see that
   skill's pitfalls — do NOT use the api.notify/scripts path locally).

## Where artifacts land — LOCAL Desktop ONLY, never OneDrive (Ford, hard rule)
Any build/zip/folder Ford has to grab or load goes to his REAL local Desktop:
`/mnt/c/Users/fordg/Desktop/` (Windows `C:\Users\fordg\Desktop`). NOT the
OneDrive-redirected `/mnt/c/Users/fordg/OneDrive/Desktop/`. Both paths exist on
his machine; saving to OneDrive drew an immediate "never onedrive" correction
(Jun 2026). When handing him an unpacked extension to load, copy the folder there
(`rm -rf` the dest first, `cp -r src/. dest/`), delete any OneDrive copy, and tell
him the `C:\Users\fordg\Desktop\...` path. NOTE: `scripts/build_extension_zip.sh`
historically dropped copies on BOTH desktop roots — when using it, remove the
OneDrive copy afterward (or fix the script) so only the local-Desktop artifact
remains.

## Notes

- Release tag convention is `ext-vX.Y.Z` (note the `ext-` prefix), asset name
  `energyagent-extension-vX.Y.Z.zip`.
- `git push` updates GitHub but does NOT auto-create releases — releases are a
  separate explicit step.
- Repo: github.com/Garface111/solar-operator ; local: /root/solar-operator.
