# Chrome Web Store — Submission Package (EnergyAgent v1.9.48)

Everything needed to publish the extension is here. **Ford does the final two
steps** (pay the one-time $5 Chrome developer fee + click *Submit for review*);
this package prepares everything up to that point.

- **Zip to upload:** `C:\Users\fordg\Desktop\energyagent-extension-v1.9.48.zip`
  (114 KB, manifest at root, MV3, no remotely-hosted code — verified).
- **Dev console:** https://chrome.google.com/webstore/devconsole

---

## ‼️ CRITICAL — an item ALREADY EXISTS. Update it, don't create a new one.

The live Array Operator dashboard hardcodes an install link to an existing Web
Store item: **id `ocohbimolfpnkjcjhiodopjjlhclinpl`** (slug `solar-operator-sync`),
wired as the "Add the 1-click helper — free →" button (`public/sandbox.js`).

Google's CRX update server recognizes this ID (`<app status="ok">`) but serves no
public version (`noupdate`) — i.e. the item was **created/uploaded but never
fully published** (draft or unlisted). So:

- **Open that existing item in the dev console and upload v1.9.48 into it** — do
  NOT create a brand-new item. A new item gets a **new random ID**, which would
  NOT match the install link baked into the live SPA → every "Add the helper"
  click would land on the wrong/old listing.
- If you can't find/access that item (wrong Google account, deleted), the
  fallback is to create a new one AND update `EXT_STORE_URL` in
  `array-operator/public/sandbox.js` to the new ID, then redeploy AO. Flag me to
  do the SPA edit if it comes to that.
- The display name will read **EnergyAgent** (from the manifest) even though the
  URL slug stays `solar-operator-sync` — that's fine, the slug is cosmetic.

---

## ⚠️ ONE BLOCKER before you can pass review — privacy policy

The Web Store reviewer compares the extension's permissions against the privacy
policy. The **live policy at https://nepooloperator.com/privacy is stale** — it's
branded "NEPOOL Operator Sync" and only describes reading Green Mountain Power.
The extension now also reads SmartHub utilities + four inverter portals
(SolarEdge, Fronius, SMA, Chint/CPS) and stores portal logins encrypted
on-device. **Submitting against the stale policy risks rejection AND misstates
to users what we do.**

→ The accurate replacement is in **`PRIVACY.md`** (this folder) and is ALREADY
publicly readable (the repo is public):
**https://github.com/Garface111/solar-operator/blob/main/extension/store-listing/PRIVACY.md**
That URL works as the listing's privacy-policy field right now — no deploy needed.

Two ways to satisfy the reviewer (Ford's call — it's outward-facing):
- **Now / zero-friction:** paste the GitHub URL above into the privacy-policy
  field. Accepted by Chrome Web Store; accurate; nothing to deploy.
- **Proper / on-domain (follow-up):** replace the live `nepooloperator.com/privacy`
  text — but it's BAKED INTO Lane A's compiled onboarding SPA bundle
  (`api/onboarding_dist/assets/index-*.js`), so it needs a Lane A SPA-source edit
  + rebuild, not a markdown swap. Coordination hand-off.

---

## Store listing fields (copy-paste)

**Item name**
```
EnergyAgent
```

**Summary** (≤132 chars)
```
Connect your solar inverter & utility accounts to EnergyAgent — your production and billing data flow in automatically. No API keys.
```

**Category:** Workflow & Planning  ·  **Language:** English

**Description**
```
EnergyAgent brings your solar array's real numbers into one dashboard — no API
keys, no spreadsheets, no logging into five different portals.

Own solar arrays? Your production data lives scattered across whatever
monitoring portal each inverter brand uses, and your billing data lives at your
utility. EnergyAgent connects them for you. Sign in to the portals you already
use, click Connect, and your inverters and meters appear in your EnergyAgent
account on their own — weather-normalized, per-inverter, in dollars.

WHAT IT CONNECTS
• Solar inverter monitoring: SolarEdge, Fronius (Solar.web), SMA (Sunny Portal /
  ennexOS), and Chint / CPS (Chint Power Systems Monitor)
• Utilities: Green Mountain Power and any NISC SmartHub co-op or municipal
  utility (used by hundreds of utilities nationwide)

HOW IT WORKS
You stay signed in to your own portal; the extension reads your own data the
same way you would by clicking around the site yourself — then sends it to your
EnergyAgent account over an encrypted connection. We never see or store your
portal passwords on our servers. Optional on-device auto-login (encrypted, saved
only on your computer, never uploaded) keeps your live numbers fresh between
visits.

PRIVACY-FIRST
• We never sell your data.
• We only read your own utility and inverter data — nothing else from your
  browser.
• Portal passwords (if you use optional auto-login) are encrypted and stored
  ONLY on your device — never sent to our servers.
• After a capture, the extension clears the portal session cookies it used.
• Delete everything anytime by emailing admin@solaroperator.org.

EnergyAgent powers Array Operator (arrayoperator.com) and NEPOOL Operator
(nepooloperator.com).
```

---

## Privacy practices tab (this is where extensions get rejected — fill carefully)

**Single purpose**
```
EnergyAgent lets a solar-array owner connect their own utility and inverter
monitoring accounts to their EnergyAgent dashboard. When the user signs in to a
supported portal and clicks Connect, the extension reads that account's own
production and billing data from the page session and sends it to the user's
EnergyAgent account — so the data flows in automatically without API keys or
manual CSV exports.
```

**Permission justifications** (paste one per field)

| Permission | Justification |
|---|---|
| `storage` | Store the user's activation code, connection state, last-capture metadata, and (if the user opts in) the encrypted on-device auto-login vault. Local to the device. |
| `alarms` | Schedule periodic background checks: remind the user before their utility session expires, and run scheduled re-captures so live production stays current. |
| `notifications` | Tell the user when a portal session is about to expire or a re-capture is needed, so their data doesn't go stale. |
| `cookies` | Used ONLY to remove (clear) the user's own portal session cookies after a capture finishes — a privacy-hygiene step so the extension doesn't leave a lingering session. Cookie contents are never read or transmitted. |
| `scripting` | Inject the capture content script into the user's already-open portal tab when they explicitly click Connect for that vendor. |

**Host permission justification**
```
Each host is a portal the user signs into to view their OWN energy data, or an
EnergyAgent app endpoint the captured data is sent to:
• Utility portals — greenmountainpower.com, api.greenmountainpower.com,
  *.smarthub.coop: read the user's own utility billing/generation data.
• Inverter monitoring portals — monitoring.solaredge.com, *.solarweb.com,
  *.sunnyportal.com, *.sma.energy, *.chintpower.com, *.chintpowersystems.com:
  read the user's own solar production data from whichever portal their inverter
  brand uses.
• EnergyAgent app hosts — nepooloperator.com, arrayoperator.com,
  solaroperator.org, web-production-49c83.up.railway.app: POST the captured data
  to the user's own EnergyAgent account and coordinate the in-page "Connect"
  handoff.
```

**Data use certifications** (check these — all true for this extension)
- ✅ I do not sell or transfer user data to third parties outside approved use cases.
  (Only third party is Resend.com, our email-delivery service provider.)
- ✅ I do not use or transfer user data for purposes unrelated to the item's single purpose.
- ✅ I do not use or transfer user data to determine creditworthiness or for lending.

**Remotely hosted code:** No — all code ships in the package (verified: no
`eval`, no `new Function`, no external `<script src>`).

**Privacy policy URL:** `https://nepooloperator.com/privacy`
(must serve the updated `PRIVACY.md` text first — see blocker above).

---

## Screenshots (required: 1–5 at 1280×800 or 640×400)

I can't capture these without a real Chrome session with the extension loaded.
Shot list, in order of impact:

1. **The toolbar popup** — pin the extension, open the popup showing "Last
   capture" + today's count + "Open dashboard". (Load unpacked from
   `C:\Users\fordg\Desktop\Energy Agent\Archives - Extension Builds\energyagent-extension-v1.9.48\`.)
2. **The "arrays appear" moment** — the Array Operator onboarding right after
   Connect, arrays cascading in (`arrayoperator.com` onboarding). This is the
   emotional hook.
3. **The connect picker** — the AO onboarding vendor grid ("Pick your monitoring
   platform — no sign-up needed yet").
4. **A populated dashboard** — arrays connected, live power, $/yr.
5. *(optional)* The popup's auto-login panel, to show the on-device/encrypted
   privacy posture.

Tip: 1280×800 browser window, OS screenshot, crop to exactly 1280×800.

---

## Ford's submit checklist

1. Pay the one-time **$5** Chrome Web Store developer registration (if not already
   a registered developer).
2. Confirm `https://nepooloperator.com/privacy` serves the **updated** policy
   (PRIVACY.md). ← a real blocker.
3. **Open the EXISTING item `ocohbimolfpnkjcjhiodopjjlhclinpl`** (see critical note
   above) → upload `energyagent-extension-v1.9.48.zip` as a new package version.
   (Only create a new item if that one is truly inaccessible — then also update
   `EXT_STORE_URL` in the SPA.)
4. Paste the listing fields + privacy-practices answers above.
5. Add 1–5 screenshots.
6. **Submit for review.**

Notes:
- First review typically takes a few days to ~2 weeks; broad host_permissions +
  the `cookies` permission usually draw a manual review — the justifications above
  are written to answer exactly what reviewers ask.
- Nothing here charges money except the $5 registration. No code change is needed
  to submit; the zip is final.
