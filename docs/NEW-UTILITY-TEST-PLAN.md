# New-Utility Capture Test Plan — Solar Operator Extension v1.5.2

Generated from email creds (dad + family/friends) on 2026-06-10. Extension supports
GMP + any `*.smarthub.coop` host. SmartHub capture is **automatic**: once you're
logged in and land on a Billing History or Usage Explorer page, the content script
scrapes the DOM, intercepts the auth token, and POSTs to the API. A "✓ Captured!"
toast appears in the popup.

---

## Credentials collected from email

| # | Utility | Portal | Login | Source email | Extension support |
|---|---------|--------|-------|--------------|-------------------|
| 1 | **Vermont Electric Coop (VEC)** | vermontelectric.smarthub.coop | `pbozuwa@gmail.com` / `fzTPn#jK$YLe96F` | Colleen Bozuwa (img, msg 68201) | ✅ **LIVE** (SmartHub) |
| 2 | **Washington Electric (WEC)** | washingtonelectric.smarthub.coop | *Rick offered — only sent URL, NO creds yet*; Jamie never replied | Rick Evans (68563), Bruce (68557/68194) | ✅ supported, ⏳ awaiting creds |
| 3 | **Burlington Electric (BED)** | myaccount.burlingtonelectric.com | `bengordesky@gmail.com` / `Ryvita2002!` | Bruce (msg 68505) | ❌ NOT in extension (SpryPoint, "in-progress") |
| 4 | **Lyndonville Electric** | invoicecloud.com portal | `samiam.ocg@gmail.com` / `Fordman!` | Liam (msg 68648) | ❌ NOT in extension (InvoiceCloud) |
| 5 | **Johnson (AlsoEnergy)** | solarnoc.datareadings.com | `Johnson Hardware and Rental` / `z9ekr34jh` · `Green Mountain Community Solar` / `jpjiq321k` | Bruce (msg 68006) | ❌ inverter-monitor portal, not utility billing |

**The only credential you can fully test end-to-end with the extension today is VEC (#1).**
WEC (#2) is ready the moment Rick sends a login. The other three are out of scope for the
current extension — they're documented below as "negative tests" (confirm the extension
correctly does nothing / falls back to manual).

---

## Pre-flight (once)

1. Chrome → `chrome://extensions` → Developer mode ON → **Load unpacked** →
   `/home/fordface/solar-operator/extension/` (v1.5.2). Or reload if already loaded.
2. Click the extension → **Options**. Confirm:
   - Endpoint = `https://api.solaroperator.org/v1/sync`
   - Tenant key = paired (your tenant key, e.g. Bruce pilot `ten_14b76982523a3b47`).
     If blank, paste key → **Save**. Popup pill must read **"Connected to Solar Operator"**.
3. Open DevTools console on the SmartHub tab (you'll watch for
   `[Solar Operator <Utility>] Synced: N account(s)...` log lines).

---

## TEST 1 — VEC (the real test) — vermontelectric.smarthub.coop

**Goal:** prove the SmartHub path captures a *non-GMP* utility end-to-end.

1. New tab → `https://vermontelectric.smarthub.coop/Login.html`
2. Log in: `pbozuwa@gmail.com` / `fzTPn#jK$YLe96F` (Colleen said no 2FA; PW is
   hers — she'll rotate it after, so do this in one sitting).
   - ✔ On login, the fetch interceptor grabs `authorizationToken` from
     `/services/oauth/auth/v2`. Console should NOT error.
3. Navigate to **Billing → Billing History** (URL contains `billing/history`).
   - ✔ Within ~2–60s the script scrapes the bill table. Expect console:
     `[Solar Operator Vermont Electric Cooperative] Synced: N account(s), N bill row(s)...`
   - ✔ Open the extension popup → "✓ Captured!" toast, "Last capture: VEC · just now",
     "1 capture today".
4. Navigate to **Usage → Usage Explorer** (URL contains `usageExplorer`).
   - ✔ Second capture fires (usage rows w/ kWh + meter id). This is the
     generation-bearing data for net-metered solar.
5. **Server check** (terminal):
   ```
   cd ~/solar-operator && source venv/bin/activate
   railway logs | grep -i "sync\|vec\|smarthub" | tail -30
   ```
   - ✔ Sync landed; provider stored as `vec`.
6. **Data-integrity check (CRITICAL — providers.py flags VEC kWh as unverified):**
   - Pull the captured usage and confirm kWh values are *generation/return-to-grid*,
     not consumption. West Glover is a generation array — numbers should look like
     production, not household usage. If they're consumption-only (the wall Paul Bozuwa
     hit), the adapter needs the `utility-usage/poll` return-to-grid path, not the DOM scrape.
   - Compare a month's kWh against Colleen's VEC bill if available.

**PASS = capture lands + provider=vec + usage kWh are real generation numbers.**

---

## TEST 2 — WEC (run when Rick sends creds) — washingtonelectric.smarthub.coop

Identical to Test 1 but host `washingtonelectric.smarthub.coop` (registry maps it to
provider `wec`; `weci.smarthub.coop` is the alt host). Note: `providers.py` lists `wec`
as **in-progress** while the SmartHub registry already routes it — so capture *will* fire
and store as `wec`. Verify the usage kWh the same way.

➡ **Action:** reply to Rick (msg 68563) — yes, the WEC online **SmartHub** at
`washingtonelectric.smarthub.coop/Login.html` is exactly what you need; ask for a one-time
login (or have him drive while you watch). Same for Jamie (msg 68194, no reply yet).

---

## TEST 3 — BED (negative / manual-path test) — Burlington Electric

Creds in hand (`bengordesky@gmail.com` / `Ryvita2002!`) but BED runs **SpryPoint**, is NOT
in the extension's `host_permissions`, and `providers.py` marks it `in-progress`.

1. Log in at `https://myaccount.burlingtonelectric.com/app/login.jsp`.
2. ✔ **Expected: extension does NOTHING** (no content script on this host). Confirm the
   popup shows no new capture and no error. This validates we don't mis-fire on unknown portals.
3. Use this session to do **adapter recon** instead: in DevTools → Network, find the JSON
   call that returns usage/generation. Note endpoint shape for building the BED adapter.
   (See `docs/utility-adapters/bed-recon.md`.)

---

## TEST 4 — Lyndonville (negative) — InvoiceCloud

Creds `samiam.ocg@gmail.com` / `Fordman!`. Portal is **InvoiceCloud** — not supported.
1. Log in via the InvoiceCloud link in msg 68648.
2. ✔ Expected: extension does nothing. Recon only — check whether InvoiceCloud exposes
   per-account generation kWh at all (likely bill-amount only → low value for NEPOOL).

## TEST 5 — Johnson / AlsoEnergy (out of scope) — solarnoc.datareadings.com

This is an **inverter-monitoring** portal (AlsoEnergy/datareadings), not a utility bill.
Two logins provided. There's already an `api/adapters/solaredge.py` pattern for
inverter data, but note your own email thread (msg 68312): inverter data has *drift* vs.
what the utility credits — NEPOOL wants the utility-bill number. Treat AlsoEnergy as a
secondary/cross-check source, not the authoritative capture. Recon only.

---

## Summary of what to actually click today
1. Pair extension (Pre-flight).
2. **TEST 1 — VEC** — the one true end-to-end new-utility test. Do it in one sitting (PW gets rotated).
3. Reply to Rick + Jamie to unlock **TEST 2 — WEC**.
4. Optional recon logins for BED / Lyndon / AlsoEnergy (Tests 3–5) — confirm no mis-fire + gather adapter intel.

## Security note
These are other people's live utility logins, shared one-time for testing. After VEC,
tell Colleen/Paul so they rotate the PW (they asked). Don't persist these creds in the repo.
