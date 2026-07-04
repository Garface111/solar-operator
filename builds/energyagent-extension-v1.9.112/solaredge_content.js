// solaredge_content.js — runs on monitoring.solaredge.com (the "one" SPA).
//
// Array Operator ZERO-KEY onboarding. When the owner clicks "Log in with
// SolarEdge" in the AO wizard, background sets a capture-intent flag and opens
// this portal. Once the owner is signed in, we READ (never generate) their
// DURABLE account API key + the site list using the page's own session cookies,
// and hand them to background → SO_CAPTURE_LANDED → the AO onboarding page,
// which runs its EXISTING /public/preview + /solaredge/connect-account flow.
//
// Grounded against a live account 2026-06-14 (see solar-operator
// docs/plans/2026-06-13-extension-inverter-capture.md → "GROUNDED CONTRACT").
// Every endpoint below is session-cookie authed; a content script's fetch()
// still sends first-party cookies (credentials:"include").
//
// PRIVACY: capture runs ONLY when a recent, explicit AO "Connect SolarEdge"
// click set the intent flag — we never read the durable key on a casual visit.
// SAFETY: the api-key endpoint is GET-only here. We NEVER POST/regenerate — a
// new key invalidates the owner's existing one. The key is held only long
// enough to hand off; the extension persists nothing.

(function () {
  "use strict";
  var _SO_BROWSER = (typeof window !== "undefined" && typeof location !== "undefined");
  if (_SO_BROWSER && !/(^|\.)solaredge\.com$/.test(location.hostname)) return;

  const INTENT_KEY = "so_capture_intent";   // {vendor, ts} set by background on SO_OPEN_PORTAL
  const SYNC_INTENT_KEY = "so_sync_intent";  // {vendor: ts} per-vendor map armed by a PARALLEL Sync-all
  const INTENT_TTL_MS = 10 * 60 * 1000;      // only act on a recent, explicit click
  const POLL_INTERVAL_MS = 4000;
  const MAX_POLLS = 30;                       // ~2 min for the owner to finish signing in
  let polls = 0;
  let lastHash = null;
  let lastLoginState = null;
  let done = false;

  async function hashString(s) {
    const buf = new TextEncoder().encode(String(s));
    const d = await crypto.subtle.digest("SHA-1", buf);
    return Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  async function getJson(url, opts) {
    const r = await fetch(url, Object.assign({ credentials: "include" }, opts || {}));
    if (!r.ok) throw new Error(url + " -> " + r.status);
    return r.json();
  }
  async function isSignedIn() {
    try {
      const r = await fetch("/services/cni/ui-api/user-info", { credentials: "include" });
      return r.ok;
    } catch (_) { return false; }
  }
  function broadcastLoginState(state) {
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED", provider: "solaredge",
      state, url: location.href, at: new Date().toISOString(),
    }, () => void chrome.runtime.lastError);
  }

  // Read (never generate) the durable account API key. The endpoint may return
  // JSON ({apiKey|key|...}) or a bare string — handle both, defensively.
  // PURE: given the raw api-key endpoint body (string), return the durable key or
  // null. Handles a JSON string, a JSON object with a *key*-ish field (top-level or
  // under .data), or a bare ~32-char alphanumeric token. Extracted so the test
  // harness can exercise every shape without a live fetch.
  function _parseApiKey(text) {
    const trimmed = String(text == null ? "" : text).trim();
    if (!trimmed) return null;
    try {
      const j = JSON.parse(trimmed);
      if (typeof j === "string") return j.trim() || null;
      if (j && typeof j === "object") {
        const scan = (o) => {
          for (const k of Object.keys(o)) {
            if (/key/i.test(k) && typeof o[k] === "string" && o[k].trim()) return o[k].trim();
          }
          return null;
        };
        return scan(j) || (j.data && typeof j.data === "object" ? scan(j.data) : null);
      }
    } catch (_) { /* not JSON — treat as a bare token */ }
    // SolarEdge account keys are ~32 alphanumeric chars.
    return /^[A-Z0-9]{16,}$/i.test(trimmed) ? trimmed : null;
  }
  async function readApiKey(accountUuid) {
    const r = await fetch("/services/account-admin/accounts/" + accountUuid + "/api-key", { credentials: "include" });
    if (!r.ok) return null;
    return _parseApiKey(await r.text());
  }

  // PURE: map the searchSites  rows to our site shape. Extracted for tests.
  function _mapSites(page) {
    return (Array.isArray(page) ? page : []).map((s) => ({
      site_id: s.solarFieldId,
      name: s.name,
      peak_power_kw: s.peakPower,
      status: s.status,
      inverter_count: s.inverterCount,
    }));
  }

  async function captureFlow() {
    const userInfo = await getJson("/services/cni/ui-api/user-info");
    const accts = await getJson("/services/account-admin/accounts?page=1&size=20");
    const items = (accts && accts.items) || [];
    if (!items.length) throw new Error("no accounts");
    const acct = items.find((a) => a.accountId === userInfo.accountId) || items[0];
    const uuid = acct.accountUuid;
    if (!uuid) throw new Error("no accountUuid");
    const apiKey = await readApiKey(uuid);

    let sites = [];
    try {
      const sl = await getJson("/services/sitelist/searchSites?v=" + Date.now(), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      sites = _mapSites((sl && sl.page) || []);
    } catch (_) { /* site list is a bonus; the key is what matters */ }

    return {
      provider: "solaredge",
      capturedAt: new Date().toISOString(),
      apiKey: apiKey || null,
      accountUuid: uuid,
      accountId: acct.accountId || userInfo.accountId || null,
      accountName: acct.accountName || null,
      user: {
        email: userInfo.email || null,
        firstName: userInfo.firstname || null,
        lastName: userInfo.lastname || null,
      },
      sites,
    };
  }

  function hasIntent() {
    return new Promise((res) => {
      try {
        chrome.storage.local.get([INTENT_KEY, SYNC_INTENT_KEY], (s) => {
          const it = s && s[INTENT_KEY];
          const single = !!(it && it.vendor === "solaredge" && (Date.now() - (it.ts || 0)) < INTENT_TTL_MS);
          const sy = s && s[SYNC_INTENT_KEY];
          const syTs = sy && sy.solaredge;
          const sync = !!(syTs && (Date.now() - syTs) < INTENT_TTL_MS);
          res(single || sync);
        });
      } catch (_) { res(false); }
    });
  }
  function clearIntent() {
    try { chrome.storage.local.remove(INTENT_KEY); } catch (_) {}
    try {
      chrome.storage.local.get(SYNC_INTENT_KEY, (s) => {
        const sy = s && s[SYNC_INTENT_KEY];
        if (!sy || sy.solaredge == null) return;   // clear ONLY our vendor so parallel siblings survive
        delete sy.solaredge;
        try { chrome.storage.local.set({ [SYNC_INTENT_KEY]: sy }); } catch (_) {}
      });
    } catch (_) {}
  }

  async function tick() {
    if (done) return;
    polls++;
    if (!(await hasIntent())) return;            // no explicit AO click → never touch the key
    if (!(await isSignedIn())) { broadcastLoginState("login_required"); return; }
    broadcastLoginState("signed_in");
    let payload;
    try { payload = await captureFlow(); } catch (_) { return; }   // retry on next tick
    if (!payload.apiKey && !(payload.sites || []).length) return;  // nothing usable yet
    const h = await hashString((payload.apiKey || "") + "|" + (payload.accountUuid || ""));
    if (h === lastHash) return;
    lastHash = h;
    done = true;
    clearIntent();
    chrome.runtime.sendMessage({ type: "SOLAREDGE_CAPTURED", payload }, () => void chrome.runtime.lastError);
  }

  // TEST HOOK (browser-inert) — see extension/tests/. No-op in a browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { _parseApiKey, _mapSites };
  }

  if (_SO_BROWSER) {
    tick();
    const iv = setInterval(() => {
      if (done || polls >= MAX_POLLS) { clearInterval(iv); return; }
      tick();
    }, POLL_INTERVAL_MS);
  }
})();
