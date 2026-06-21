# CHINT / CPS — honest capture status + the exact live-verification ask (Jun 21 2026)

This corrects two stale beliefs and names the real remaining gap. Read alongside
`chint-portal-api-contract.md` (which still carries a v1.9.20 "BLOCKED" status
that later builds moved past) and `inverter-vendor-status.md`.

## Two portals, not one — keep them separate

| Portal | Host | Capture status |
|---|---|---|
| **CPS (Chint Power Systems, North America)** | `monitor.chintpowersystems.com:8443` | **HAR-grounded + parser shipped.** This is Bruce's portal. |
| **Chint global (Fomware white-label)** | `solar.chintpower.com` | **Ungrounded. No content script runs there** — host-permission only. |

The content script guards on `chintpowersystems.com` (`chint_content.js:32`) and
the manifest `content_scripts` match is `monitor.chintpowersystems.com` only. So
"Chint global" is **not** captured today — we have no live `solar.chintpower.com`
account and have never observed its SPA. Do not write a parser for it blind; it's
a different SPA than CPS even if it shares the Fomware backend.

## What is actually TRUE for CPS (verified from code + HARs)

- **Endpoints + JSON shapes are HAR-grounded** against Bruce's live "Londonderry
  186" account (2026-06-16) — NOT fabricated. See `chint-portal-api-contract.md`.
- **The parser is built and shipped** (v1.9.12 → v1.9.21 passive-observation →
  v1.9.39 daily-history backfill). Approach: `chint_inject.js` (MAIN world) hooks
  the app's OWN XHR/fetch responses for `site/retrieve` + `busTypeDevices` and
  relays them to `chint_content.js`, which assembles the per-inverter payload and
  POSTs `CHINT_CAPTURED`. Zero auth replay (the token is CryptoJS-encrypted +
  per-request bound, so replay is dead — this design sidesteps it).
- **Failure is honest**: on timeout/no-data it posts `CHINT_CAPTURE_FAILED` with a
  reason, and the AO onboarding shows "Didn't see your Chint inverters yet — make
  sure you're signed in at monitor.chintpowersystems.com and click into each
  site." The vendor card copy reads "Chint/CPS support is in final verification
  against live accounts." Both are appropriately hedged — a SOFT gate, not an
  overpromise. (No hard feature-flag: hiding a HAR-grounded, possibly-working
  vendor would cost users who CAN use it; honest copy is the right gate.)

## The REAL gap: live click-through is UNVERIFIED (and there's a yellow flag)

The capture code is complete, but I cannot confirm it actually lands on a live
session — I have no Chint/CPS account. And the git trail is ambiguous:
`v1.9.21` is committed as "working capture", but `v1.9.36` then adds "loud Chint
capture logging to pin why live power isn't flowing", which means it was STILL
being debugged after the "working" claim. So treat CPS as **code-complete,
HAR-grounded, live-capture UNPROVEN** — not "live-proven" like SolarEdge.

## EXACT ask to Ford (this is the only thing that closes it)

On the machine with **Bruce's GMCS / "GMCS Manager" CPS login**, with extension
**v1.9.48** installed:

1. Start the AO onboarding "Connect Chint" flow (so the `so_capture_intent` flag
   is armed), which opens `monitor.chintpowersystems.com`.
2. Sign in. Then **click into EACH site** (the `busTypeDevices` fetch only fires
   when a site is opened — opening one site captures all its inverters at once).
3. Open DevTools on the Chint tab → Console, and **screenshot/paste the
   `[EnergyAgent CHINT]` lines**. The two that decide everything:
   - **`API response seen: …busTypeDevices`** → the MAIN-world hooks ARE catching
     the app's fetches. If present, capture should work; if the AO modal still
     shows nothing, it's a downstream payload/post bug (easy from there).
   - **NO `API response seen` after opening a site** → the app fetches via a
     Web/Service Worker the main-page hooks can't see. Interception is exhausted;
     the honest next options are heavier (`chrome.debugger` reader — shows a
     browser banner — or ship CPS as manual/best-effort). STOP and bring the
     tradeoffs, don't keep grinding builds blind.

That one console screenshot converts CPS from "unproven" to either "live-proven"
or a pinned, named failure. **For Chint global (`solar.chintpower.com`): a
separate HAR from a real account on THAT portal is needed before any parser — do
not fabricate its endpoints.**
