// background.js — service worker.
// Receives captured tokens from content.js (GMP) and vec_content.js (VEC),
// persists locally, and POSTs to the EnergyAgent API.

// v1.9.33: client-side encrypted credential vault for portal auto-login (SoVault).
// Loaded first so it's available to all handlers. Creds are AES-GCM encrypted and
// stored ONLY in chrome.storage.local — never sent to our backend.
try { importScripts("vault.js"); } catch (e) { console.warn("[EnergyAgent] vault load failed", e); }

// v1.9.97: the SmartHub host→co-op-code registry, loaded into the SW so utility
// auto-login can resolve a *.smarthub.coop login page to the right co-op code
// (vec/wec/sh_*) and use that co-op's saved credential. The generated file is
// SW-safe (exposes self.SMARTHUB_REGISTRY + self.smartHubCodeForHost when there
// is no `window`). Best-effort: if it fails to load, utility auto-login simply
// falls back to GMP-only + the vault's grounded co-op fallback set.
try { importScripts("smarthub_registry.js"); } catch (e) { console.warn("[EnergyAgent] smarthub registry load failed", e); }

// v1.3.0: SO_PAIR / SO_STATUS_REQUEST handlers + SO_CAPTURE_LANDED +
//         SO_LOGIN_STATE broadcasts to every solaroperator.org tab so the
//         onboarding wizard can mirror live state without polling.
// v1.2.0: OPEN_UTILITY_PORTAL background-tab handler + so_bridge.js content
//         script for SPA ↔ extension postMessage.
// v1.1.0: added VEC / NISC SmartHub support (VEC_DATA_CAPTURED)
// v1.0.2: primary endpoint on api.solaroperator.org with Railway fallback
// during the CNAME transition window.
// v1.7.1: primary endpoint moved to nepooloperator.com (apex proxies /v1/* -> Railway,
// same-origin with the dashboard). Railway public domain kept as fallback. The old
// api.solaroperator.org never resolved, so traffic was running on the fallback anyway.
const PROD_ENDPOINT = "https://nepooloperator.com/v1/sync";
const FALLBACK_ENDPOINT = "https://web-production-49c83.up.railway.app/v1/sync";

// ── Whole-system error capture (v1.9.10) ─────────────────────────────────────
// Report extension errors to the shared backend /v1/client-error so they land in
// the same Sentry + alert pipeline as server + frontend errors. The SMA debugging
// saga (v1.9.2→1.9.8) would have been near-instant with this. Best-effort,
// deduped, capped — an error reporter must never itself throw or flood.
const SO_ERROR_ENDPOINT = "https://web-production-49c83.up.railway.app/v1/client-error";
const _soErrSeen = {};
let _soErrCount = 0;
function soReportError(message, stack, kind) {
  try {
    if (_soErrCount >= 25) return;
    message = String(message || "").slice(0, 500);
    stack = String(stack || "").slice(0, 4000);
    if (!message && !stack) return;
    const sig = (kind || "") + "|" + message;
    const now = Date.now();
    if (_soErrSeen[sig] && now - _soErrSeen[sig] < 60000) return;
    _soErrSeen[sig] = now;
    _soErrCount++;
    fetch(SO_ERROR_ENDPOINT, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "extension", message, stack, url: kind || "background", kind: kind || "error" }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) { /* never throw from the reporter */ }
}
try {
  self.addEventListener("error", (e) =>
    soReportError(e && e.message, e && e.error && e.error.stack, "sw-error"));
  self.addEventListener("unhandledrejection", (e) => {
    const r = e && e.reason;
    soReportError(r && r.message ? r.message : String(r), r && r.stack, "sw-rejection");
  });
} catch (_) {}

const STORAGE_KEYS = {
  ENDPOINT: "api_endpoint",
  TENANT_KEY: "tenant_key",
  LAST_SYNC: "last_sync",
  LAST_PAYLOAD: "last_payload",
  LAST_ERROR: "last_error",
  LAST_LOGIN_STATE: "last_login_state",  // v1.3.0 — most recent per-provider login state
  CAPTURES_TODAY: "captures_today",      // v1.4.0 — { date: "YYYY-MM-DD", count: N }
};

// v1.3.0: broadcast a payload to every open dashboard tab so the
// SPA can react without polling. The so_bridge.js content script picks
// these up via chrome.runtime.onMessage and re-posts to its window.
// v1.7.1: nepooloperator.com is now the primary dashboard host; solaroperator.org
// kept during the transition so in-flight users aren't cut off.
const SO_TAB_URLS = [
  "https://nepooloperator.com/*",
  "https://*.nepooloperator.com/*",
  "https://arrayoperator.com/*",
  "https://*.arrayoperator.com/*",
  "https://solaroperator.org/*",
  "https://*.solaroperator.org/*",
  "https://web-production-49c83.up.railway.app/*",
];
function broadcastToSoTabs(message, senderTab) {
  try {
    chrome.tabs.query({ url: SO_TAB_URLS }, (tabs) => {
      if (chrome.runtime.lastError) { void chrome.runtime.lastError; return; }
      for (const t of tabs || []) {
        if (typeof t.id !== "number") continue;
        chrome.tabs.sendMessage(t.id, message, () => {
          // Tab may not have so_bridge.js loaded yet (race on document_start) —
          // swallow the "Receiving end does not exist" error.
          void chrome.runtime.lastError;
        });
      }
    });
    // After a SUCCESSFUL capture from a user-initiated FOREGROUND connect, bring the
    // operator back to the Array Operator tab they started from (set in OPEN_UTILITY_PORTAL).
    // But NEVER refocus when the capture came from a background "Sync all" / recap surface
    // (Ford: Sync-all must stay on his current tab) — gate on whether the sender tab is one
    // of those. Legacy callers that pass no senderTab (GMP/SmartHub via _handleSync) keep
    // the original refocus behavior unchanged.
    if (message && message.type === "SO_CAPTURE_LANDED" && message.ok && !message._noReturn) {
      maybeReturnAfterCapture(message.provider, senderTab);
    }
  } catch (e) {
    console.warn("[EnergyAgent] broadcastToSoTabs failed:", e);
  }
}

// Focus the AO tab the operator launched a foreground vendor connect from, then
// clear it (one-shot). No-op if none stored or it's stale (>10 min). This is the
// "bring me back to our tab once the data's collected" behavior.
function focusReturnTab() {
  try {
    chrome.storage.local.get("so_return_tab", (st) => {
      const rt = st && st.so_return_tab;
      if (!rt || typeof rt.tabId !== "number") return;
      if (Date.now() - (rt.ts || 0) > 10 * 60 * 1000) { chrome.storage.local.remove("so_return_tab"); return; }
      try { chrome.tabs.update(rt.tabId, { active: true }, () => void chrome.runtime.lastError); } catch (_) {}
      if (typeof rt.windowId === "number") {
        try { chrome.windows.update(rt.windowId, { focused: true }, () => void chrome.runtime.lastError); } catch (_) {}
      }
      chrome.storage.local.remove("so_return_tab");
    });
  } catch (_) { /* non-fatal */ }
}

// Decide whether a successful capture should bring the operator back to the AO tab.
// A foreground "Open <vendor> to sync" records so_return_tab {tabId, vendor}. We return
// them when THAT vendor's data lands — whether the foreground portal tab captured it OR a
// concurrent BACKGROUND recapture did (clicking "Open SMA to sync" arms SMA live-mode, so a
// background tick often lands the data ~1-4 min later, while the foreground tab's own
// capture gets pre-empted). Without vendor-scoping, that background capture is treated as a
// "hidden" surface and the return is suppressed → the operator is stranded on the vendor
// page (the "it synced but never took me back" bug). A Sync-all records NO return tab (and
// clears any stale one when it starts), so it still never pulls focus. Legacy captures with
// a return tab but no recorded vendor keep the original hidden-surface gate.
function maybeReturnAfterCapture(provider, senderTab) {
  try {
    chrome.storage.local.get("so_return_tab", (st) => {
      const rt = st && st.so_return_tab;
      if (!rt || typeof rt.tabId !== "number") return;                 // nothing pending
      if (Date.now() - (rt.ts || 0) > 10 * 60 * 1000) {                // stale → drop
        chrome.storage.local.remove("so_return_tab"); return;
      }
      const prov = String(provider || "").toLowerCase();
      if (rt.vendor) {
        // Vendor-scoped: ONLY this vendor's capture returns (and clears) it — even from a
        // hidden background surface, because the operator explicitly asked for it and is
        // waiting. A different vendor's capture leaves the pending return untouched.
        if (prov && prov === rt.vendor) focusReturnTab();
        return;
      }
      // Legacy (no vendor recorded): original hidden-surface gate.
      const tid = senderTab && typeof senderTab.id === "number" ? senderTab.id : null;
      if (tid != null && typeof self.__soIsHiddenSyncSurface === "function") {
        Promise.resolve(self.__soIsHiddenSyncSurface(tid)).then((hidden) => { if (!hidden) focusReturnTab(); });
      } else {
        focusReturnTab();
      }
    });
  } catch (_) { /* non-fatal */ }
}

async function getSettings() {
  const s = await chrome.storage.local.get([
    STORAGE_KEYS.ENDPOINT,
    STORAGE_KEYS.TENANT_KEY,
  ]);
  return {
    endpoint: s[STORAGE_KEYS.ENDPOINT] || PROD_ENDPOINT,
    tenantKey: s[STORAGE_KEYS.TENANT_KEY] || "",
  };
}

async function postSync(payload) {
  const { endpoint, tenantKey } = await getSettings();
  const headers = { "Content-Type": "application/json" };
  if (tenantKey) headers["Authorization"] = `Bearer ${tenantKey}`;

  async function tryPost(url) {
    const res = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${await res.text().catch(() => "")}`);
    }
    return res.json().catch(() => ({}));
  }

  try {
    return await tryPost(endpoint);
  } catch (e) {
    // Fall back to Railway origin if the primary (api.solaroperator.org) fails
    // — covers the pre-CNAME window and DNS hiccups. Only retry if endpoint
    // is the default PROD_ENDPOINT (user-customized endpoints don't fall back).
    if (endpoint === PROD_ENDPOINT && FALLBACK_ENDPOINT !== endpoint) {
      console.warn("[EnergyAgent] primary endpoint failed, retrying fallback:", e.message);
      return await tryPost(FALLBACK_ENDPOINT);
    }
    throw e;
  }
}

// v1.4.0: increment the captures-today counter (resets each calendar day).
async function _incrementCapturesToday() {
  const todayStr = new Date().toISOString().slice(0, 10);
  const s = await chrome.storage.local.get(STORAGE_KEYS.CAPTURES_TODAY);
  const prev = s[STORAGE_KEYS.CAPTURES_TODAY];
  const count = (prev && prev.date === todayStr) ? prev.count + 1 : 1;
  await chrome.storage.local.set({
    [STORAGE_KEYS.CAPTURES_TODAY]: { date: todayStr, count },
  });
}

// Shared sync handler — used by both GMP and VEC message types.
// Stores capture metadata, POSTs to /v1/sync, updates badge.
async function _handleSync(payload, tokenHash, sendResponse) {
  try {
    await chrome.storage.local.set({
      [STORAGE_KEYS.LAST_PAYLOAD]: {
        capturedAt: payload.capturedAt,
        provider: payload.provider || "gmp",
        accountCount: (payload.accounts || []).length,
        username: (payload.user || {}).username || null,
        tokenExpires: (payload.auth || {}).apiTokenExpires || null,
        tokenHash,
      },
    });

    const settings = await getSettings();
    if (!settings.tenantKey) {
      await chrome.storage.local.set({
        [STORAGE_KEYS.LAST_SYNC]: {
          ok: true,
          at: new Date().toISOString(),
          mode: "local-only",
          message: "Captured locally. Set tenant key in options to enable sync.",
        },
        [STORAGE_KEYS.LAST_ERROR]: null,
      });
      chrome.action.setBadgeText({ text: "✓" });
      chrome.action.setBadgeBackgroundColor({ color: "#666" });
      sendResponse({ ok: true, endpoint: "local-only" });
      return;
    }

    const result = await postSync(payload);
    const at = new Date().toISOString();
    await chrome.storage.local.set({
      [STORAGE_KEYS.LAST_SYNC]: {
        ok: true,
        at,
        endpoint: settings.endpoint,
        result,
      },
      [STORAGE_KEYS.LAST_ERROR]: null,
    });
    await _incrementCapturesToday();
    chrome.action.setBadgeText({ text: "✓" });
    chrome.action.setBadgeBackgroundColor({ color: "#2e6b3a" });
    // v1.3.0: tell every open solaroperator.org tab so the onboarding wizard
    // can auto-advance the moment a capture lands.
    const isNew = !result || result.is_new_client !== false;
    const capturedMsg = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: payload.provider || "gmp",
      accountCount: (payload.accounts || []).length,
      at,
      is_new_client: isNew,
      result: (result && result.result) || "created",
      client_name: (result && result.client && result.client.name) || null,
      residentialCount: (result && result.residential_count) || 0,
    };
    broadcastToSoTabs(capturedMsg);
    // v1.4.0: also notify the popup if it is open.
    chrome.runtime.sendMessage(capturedMsg, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true, endpoint: settings.endpoint });
  } catch (e) {
    const at = new Date().toISOString();
    await chrome.storage.local.set({
      [STORAGE_KEYS.LAST_ERROR]: { at, message: String(e) },
    });
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#c97a3d" });
    const failedMsg = {
      type: "SO_CAPTURE_LANDED",
      ok: false,
      provider: (payload && payload.provider) || "gmp",
      accountCount: 0,
      at,
      error: String(e),
    };
    broadcastToSoTabs(failedMsg);
    // v1.4.0: also notify the popup if it is open.
    chrome.runtime.sendMessage(failedMsg, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: false, error: String(e) });
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (
    msg.type === "GMP_TOKEN_CAPTURED" ||
    msg.type === "VEC_DATA_CAPTURED" ||
    msg.type === "SMARTHUB_DATA_CAPTURED"
  ) {
    _handleSync(msg.payload, msg.tokenHash, sendResponse);
    return true; // keep channel open for async sendResponse
  }

  // v1.8.0: SolarEdge inverter capture for Array Operator. Unlike the utility
  // captures, this does NOT POST to /v1/sync — solaredge_content.js read the
  // owner's DURABLE account API key + site list from the logged-in portal, and
  // we hand them straight to the AO onboarding page via SO_CAPTURE_LANDED. The
  // page then runs its existing /public/preview + /solaredge/connect-account
  // flow. (Reuses the array-operator backend untouched.)
  if (msg.type === "SOLAREDGE_CAPTURED") {
    const p = msg.payload || {};
    const landed = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: "solaredge",
      apiKey: p.apiKey || null,
      sites: Array.isArray(p.sites) ? p.sites : [],
      accountCount: Array.isArray(p.sites) ? p.sites.length : 0,
      accountName: p.accountName || null,
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(landed, sender && sender.tab);
    chrome.runtime.sendMessage(landed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous response
  }
  // v1.9.0: Fronius (Solar.web) inverter capture for Array Operator. Fronius's
  // Solar.web Query API is a paid business API NOT offered in the USA, so there
  // is no key for the backend to pull with. Instead solarweb_content.js reads
  // the owner's LIVE READINGS from the logged-in portal and we hand them to the
  // AO page via SO_CAPTURE_LANDED. The page POSTs them to
  // /v1/array-owners/inverter-capture with its session token (page-side auth,
  // same as the SolarEdge connect flow).
  if (msg.type === "FRONIUS_CAPTURED") {
    const p = msg.payload || {};
    const landed = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: "fronius",
      sites: Array.isArray(p.sites) ? p.sites : [],
      accountCount: Array.isArray(p.sites) ? p.sites.length : 0,
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(landed, sender && sender.tab);
    chrome.runtime.sendMessage(landed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous response
  }
  // v1.9.2: SMA (ennexOS / Sunny Portal) per-inverter capture for Array Operator.
  // SMA's official API needs developer-app registration + owner consent, so we
  // read the owner's per-inverter readings from the logged-in portal instead and
  // hand them to the AO page via SO_CAPTURE_LANDED (same shape as Fronius).
  if (msg.type === "SMA_CAPTURED") {
    const p = msg.payload || {};
    const landed = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: "sma",
      sites: Array.isArray(p.sites) ? p.sites : [],
      accountCount: Array.isArray(p.sites) ? p.sites.length : 0,
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(landed, sender && sender.tab);
    chrome.runtime.sendMessage(landed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous response
  }
  // v1.9.5: SMA capture gave up — relay the REASON to the AO page so its spinner
  // resolves into a real error instead of hanging forever.
  if (msg.type === "SMA_CAPTURE_FAILED") {
    const failed = {
      type: "SO_CAPTURE_FAILED",
      ok: false,
      provider: "sma",
      reason: String(msg.reason || "unknown"),
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(failed);
    chrome.runtime.sendMessage(failed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous
  }
  // v1.9.11: Chint / CPS (solar.chintpower.com, a Fomware white-label) per-inverter
  // capture for Array Operator. CHINT publishes no owner API key, so chint_content.js
  // reads the owner's live readings from the logged-in portal and we hand them to the
  // AO page via SO_CAPTURE_LANDED (same shape as Fronius/SMA). NOTE: the chint
  // extraction endpoints are not yet grounded against a live account — the content
  // script fails gracefully (CHINT_CAPTURE_FAILED) until they're verified.
  if (msg.type === "CHINT_CAPTURED") {
    const p = msg.payload || {};
    const tid = (sender && sender.tab && typeof sender.tab.id === "number") ? sender.tab.id : null;
    const landed = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: "chint",
      sites: Array.isArray(p.sites) ? p.sites : [],
      accountCount: Array.isArray(p.sites) ? p.sites.length : 0,
      at: new Date().toISOString(),
      // Only the FINAL (walk-complete) emit triggers the return-to-AO. Per-site partial emits
      // during a multi-site walk just stream the arrays — returning on the first one would
      // strand the owner (the SPA keeps yanking its tab forward as the walk continues).
      _noReturn: !p.walkComplete,
    };
    if (p.walkComplete && tid != null) {
      // Read the pending return BEFORE the broadcast clears it: if a return to the AO tab is
      // pending for chint, this Chint tab is an onboarding foreground connect (not the owner's
      // own Chint tab). Return them, then CLOSE this tab so Chint's SPA can't pull itself back
      // to the foreground after the route walk finishes (the "never brought me back" bug).
      chrome.storage.local.get("so_return_tab", (st) => {
        const rt = st && st.so_return_tab;
        const isOnbConnect = !!(rt && rt.vendor === "chint" && rt.tabId !== tid);
        const returnTabId = isOnbConnect && typeof rt.tabId === "number" ? rt.tabId : null;
        const returnWindowId = isOnbConnect && typeof rt.windowId === "number" ? rt.windowId : null;
        broadcastToSoTabs(landed, sender && sender.tab);   // fires the return (focusReturnTab)
        if (isOnbConnect) {
          setTimeout(() => {
            try { chrome.tabs.remove(tid, () => void chrome.runtime.lastError); } catch (_) {}
            // Re-assert focus on the AO tab AFTER closing Chint — closing the (possibly-foreground)
            // Chint tab must never leave the owner stranded on some other tab.
            if (returnTabId != null) {
              try { chrome.tabs.update(returnTabId, { active: true }, () => void chrome.runtime.lastError); } catch (_) {}
              if (returnWindowId != null) { try { chrome.windows.update(returnWindowId, { focused: true }, () => void chrome.runtime.lastError); } catch (_) {} }
            }
          }, 700);
        }
      });
    } else {
      broadcastToSoTabs(landed, sender && sender.tab);
    }
    chrome.runtime.sendMessage(landed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous response
  }
  // v1.9.11: Chint capture gave up — relay the REASON to the AO page so its spinner
  // resolves into a real (honest) error instead of hanging forever.
  if (msg.type === "CHINT_CAPTURE_FAILED") {
    const failed = {
      type: "SO_CAPTURE_FAILED",
      ok: false,
      provider: "chint",
      reason: String(msg.reason || "unknown"),
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(failed);
    chrome.runtime.sendMessage(failed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous
  }
  // v1.9.23: GMP utility-meter PRODUCTION capture for Array Operator. Distinct
  // from the GMP bill capture (GMP_TOKEN_CAPTURED → /v1/sync). gmp_meter_content.js
  // read the owner's SOLAR GENERATION from the GMP usage API (via GMP_FETCH_USAGE
  // below) and hands it to the AO page via SO_CAPTURE_LANDED. The page POSTs the
  // accounts to /v1/array-owners/utility-meter-capture with its session token.
  if (msg.type === "GMP_METER_CAPTURED") {
    const p = msg.payload || {};
    const accounts = Array.isArray(p.accounts) ? p.accounts : [];
    const landed = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: "gmp",
      kind: "utility_meter",
      accounts,
      accountCount: accounts.length,
      auth: p.auth || null,
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(landed, sender && sender.tab);
    chrome.runtime.sendMessage(landed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous response
  }
  // v1.9.26: SmartHub (VEC / WEC) utility-meter PRODUCTION capture for Array
  // Operator — CLIENT-SIDE pull. smarthub_content.js pulled the daily generation
  // itself (same-origin, riding the owner's session cookie — the backend can't
  // replay an httpOnly cookie, which is why the v1.9.25 token-relay design failed)
  // and hands us the assembled per-account daily[] series. We relay it to the AO
  // page via SO_CAPTURE_LANDED; the page POSTs the accounts to the EXISTING
  // /v1/array-owners/utility-meter-capture endpoint (the proven GMP daily path).
  if (msg.type === "SMARTHUB_METER_GEN_CAPTURED") {
    const accounts = Array.isArray(msg.accounts) ? msg.accounts : [];
    const provider = msg.provider || "vec";
    const landed = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider,
      kind: "utility_meter",
      accounts,
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(landed, sender && sender.tab);
    chrome.runtime.sendMessage(landed, () => { void chrome.runtime.lastError; });
    // NEPOOL path: there is no Array Operator page open to POST the capture, but
    // the extension is paired to a tenant. POST the daily generation straight to
    // the dual-auth endpoint with the stored tenant key so VEC/WEC generation
    // lands in DailyGeneration → NEPOOL reports. ADDITIVE: the AO relay above
    // still runs for Array Operator owners (idempotent if both fire).
    (async () => {
      try {
        const { tenantKey, endpoint } = await getSettings();
        if (!tenantKey || accounts.length === 0) return;
        const base = (endpoint || PROD_ENDPOINT).replace(/\/v1\/sync$/, "");
        const r = await fetch(`${base}/v1/array-owners/utility-meter-capture`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Authorization": `Bearer ${tenantKey}` },
          body: JSON.stringify({ provider, accounts }),
        });
        console.log("[EnergyAgent] smarthub meter-capture POST ->", r.status, r.ok ? "ok" : "FAIL");
      } catch (e) {
        console.warn("[EnergyAgent] smarthub meter-capture POST threw:", e && e.message || e);
      }
    })();
    sendResponse({ ok: true });
    return; // synchronous response
  }
  // SmartHub meter capture gave up — relay the REASON so the AO spinner resolves
  // into a real error instead of hanging forever.
  if (msg.type === "SMARTHUB_METER_FAILED") {
    const failed = {
      type: "SO_CAPTURE_FAILED",
      ok: false,
      provider: msg.provider || "vec",
      kind: "utility_meter",
      reason: String(msg.reason || "unknown"),
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(failed);
    chrome.runtime.sendMessage(failed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous
  }
  // v1.9.23: GMP meter capture gave up — relay the REASON to the AO page so its
  // spinner resolves into a real error instead of hanging forever.
  if (msg.type === "GMP_METER_CAPTURE_FAILED") {
    const failed = {
      type: "SO_CAPTURE_FAILED",
      ok: false,
      provider: "gmp",
      kind: "utility_meter",
      reason: String(msg.reason || "unknown"),
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(failed);
    chrome.runtime.sendMessage(failed, () => { void chrome.runtime.lastError; });
    sendResponse({ ok: true });
    return; // synchronous
  }
  // v1.9.23: cross-origin proxy for the GMP usage API. The page is
  // greenmountainpower.com but the API is api.greenmountainpower.com — a
  // credentialed content-script fetch can CORS-block. The service worker holds
  // host_permissions for api.greenmountainpower.com, so it makes the
  // authenticated GETs CORS-free. gmp_meter_content.js passes the owner's JWT
  // (from localStorage gmp-vue.user.apitoken); we enumerate energyAccounts then
  // read each account's usage summary (the solar-generation signal). Mirrors the
  // SMA_API_GET proxy. Read-only: we only ever GET the GMP usage API.
  if (msg.type === "GMP_FETCH_USAGE") {
    const jwt = String(msg.jwt || "");
    if (!jwt) { sendResponse({ ok: false, error: "missing-jwt" }); return; }
    const API = "https://api.greenmountainpower.com/api/v2";
    const headers = {
      "Authorization": `Bearer ${jwt}`,
      "Accept": "application/json",
      "GMP-Source": "web",
    };
    const gmpGet = async (path) => {
      const r = await fetch(`${API}${path}`, { headers });
      if (!r.ok) { const e = new Error(`HTTP ${r.status}`); e.status = r.status; throw e; }
      return r.json();
    };
    // GMP usage local-time format for the date-range query params (no tz suffix
    // needed; the API interprets them in the account's local time).
    const fmtDate = (d) => d.toISOString().slice(0, 19);
    (async () => {
      try {
        const current = await gmpGet("/users/current");
        // energyAccounts live under customData on /users/current.
        const cd = (current && current.customData) || {};
        const energyAccounts = Array.isArray(cd.energyAccounts) ? cd.energyAccounts
          : (Array.isArray(current.energyAccounts) ? current.energyAccounts : []);
        const accounts = [];
        for (const ea of energyAccounts) {
          const acctNum = String(ea.accountNumber || "").trim();
          if (!acctNum) continue;
          let summary = {};
          try {
            summary = await gmpGet(`/usage/${acctNum}/summary`);
          } catch (e) {
            // A single account 401/404 shouldn't sink the whole capture — record
            // it with an empty summary so the backend marks it no-generation.
            console.warn("[so] GMP summary fetch failed for", acctNum, String(e && e.message));
            summary = {};
          }
          // Only the SOLAR accounts produce generation — Bruce has 48 GMP accounts
          // and most are non-solar homes/pumps. Skip the extra /daily call unless
          // the summary shows real generation (isNetMetered OR a positive
          // grossGenerated / returnedGeneration), so we don't fire 48 daily calls.
          const grossGen = Number(summary && summary.totalGrossGenerated) || 0;
          const sentGrid = Number(summary && summary.totalGenerationSentToGrid) || 0;
          const usedHome = Number(summary && summary.totalGenerationUsedByHome) || 0;
          const isSolar = !!(summary && (summary.isNetMetered || grossGen > 0 || sentGrid > 0 || usedHome > 0));

          let daily = [];
          if (isSolar) {
            // MULTI-YEAR backfill. GMP's /daily endpoint accepts arbitrary
            // historical ranges (grounded via HAR Jun 2026: a 2019 account
            // returned full daily data for 2019..now; pre-online years return a
            // ~144-byte empty shell). So we walk backward ONE CALENDAR YEAR at a
            // time, collecting returnedGeneration (= daily solar production),
            // and STOP for this account as soon as a year yields zero generation
            // rows (its pre-online void) — no wasted calls into empty history.
            // The backend ingest is idempotent per (array, day) with max-kWh, so
            // re-running only fills gaps; capped at MAX_YEARS for safety.
            const MAX_YEARS = 12;
            const nowY = new Date().getUTCFullYear();
            const parseYear = async (yr) => {
              const start = `${yr}-01-01T00:00:00`;
              const end = (yr === nowY)
                ? fmtDate(new Date())
                : `${yr}-12-31T23:59:59`;
              const dResp = await gmpGet(
                `/usage/${acctNum}/daily?startDate=${start}&endDate=${end}&temp=f`
              );
              const intervals = (dResp && Array.isArray(dResp.intervals)) ? dResp.intervals : [];
              const out = [];
              for (const iv of intervals) {
                for (const v of (iv.values || [])) {
                  const g = (v && v.returnedGeneration != null) ? Number(v.returnedGeneration) : null;
                  if (g != null && isFinite(g) && g > 0 && v.date) {
                    out.push({ date: v.date, generated_kwh: g });
                  }
                }
              }
              return out;
            };
            try {
              let emptyStreak = 0;
              for (let i = 0; i < MAX_YEARS; i++) {
                const yr = nowY - i;
                let yrRows = [];
                try {
                  yrRows = await parseYear(yr);
                } catch (e) {
                  // A single bad year shouldn't sink the backfill; note + continue.
                  console.warn("[so] GMP daily year", yr, "failed for", acctNum, String(e && e.message));
                  yrRows = [];
                }
                if (yrRows.length) {
                  daily = daily.concat(yrRows);
                  emptyStreak = 0;
                } else {
                  // The CURRENT year can legitimately have 0 gen rows early in
                  // Jan; don't treat that as the history floor. For any prior
                  // year, an empty result means we've walked past this account's
                  // online date — stop.
                  if (yr < nowY) {
                    emptyStreak++;
                    if (emptyStreak >= 1) break;
                  }
                }
                await new Promise(r => setTimeout(r, 250)); // polite pacing
              }
            } catch (e) {
              console.warn("[so] GMP multi-year backfill failed for", acctNum, String(e && e.message));
            }
            if (daily.length) {
              console.log("[so] GMP backfill", acctNum, "→", daily.length, "daily rows,",
                          (daily[daily.length - 1] || {}).date, "..", (daily[0] || {}).date);
            }
          }
          accounts.push({
            account_number: acctNum,
            nickname: ea.nickname || null,
            summary,
            daily,
          });
        }
        sendResponse({ ok: true, accounts });
      } catch (e) {
        sendResponse({ ok: false, status: e && e.status, error: String((e && e.message) || e) });
      }
    })();
    return true; // async sendResponse
  }
  // Access-Control-Allow-Origin:* — which the browser refuses to pair with a
  // credentialed content-script fetch (CORS block → the capture stalled). The
  // service worker, holding host_permissions for uiapi.sunnyportal.com, can make
  // the credentialed cross-origin request CORS-free. So sunnyportal_content.js
  // routes every uiapi GET through here.
  if (msg.type === "SMA_API_GET") {
    const url = String(msg.url || "");
    // Hard allowlist — only ever proxy the SMA UI API, never an arbitrary URL.
    if (!/^https:\/\/uiapi\.sunnyportal\.com\//.test(url)) {
      sendResponse({ ok: false, error: "url-not-allowed" });
      return; // sync
    }
    (async () => {
      try {
        const r = await fetch(url, {
          credentials: "include",
          headers: { "Accept": "application/json" },
        });
        if (!r.ok) { sendResponse({ ok: false, status: r.status }); return; }
        const data = await r.json().catch(() => null);
        sendResponse({ ok: true, status: r.status, data });
      } catch (e) {
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      }
    })();
    return true; // async sendResponse
  }
  // the deployment to the drift radar (best-effort, fire and forget).
  if (msg.type === "SMARTHUB_SCRAPE_EMPTY") {
    (async () => {
      try {
        const { tenantKey, endpoint } = await getSettings();
        if (!tenantKey) return;
        const base = endpoint.replace(/\/v1\/sync$/, "");
        await fetch(`${base}/v1/extension/scrape-miss`, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${tenantKey}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(msg),
        });
      } catch (_) { /* telemetry is never fatal */ }
    })();
    sendResponse({ ok: true });
    return;
  }
  // v1.2.0: SPA asks the extension to open the utility portal in a
  // background tab — gives content.js a chance to capture without
  // stealing the operator's focus from solaroperator.org.
  if (msg.type === "OPEN_UTILITY_PORTAL") {
    const url = String(msg.url || "");
    // v1.4.2: caller can opt into a foreground tab via msg.active=true.
    // Default stays false (background tab) for ambient capture flows.
    // The Add Client modal sets true because the operator is actively
    // about to sign in — making them switch tabs manually is silly.
    const active = msg.active === true;
    // Remember the Array Operator tab the operator launched this from, so we can
    // bring them back to it automatically once the vendor data lands (they're about
    // to be sent to the vendor portal in a FOREGROUND tab to log in). One-shot.
    if (active && sender && sender.tab && typeof sender.tab.id === "number") {
      // Record the VENDOR too, so the post-capture return fires when this vendor's data
      // lands even if a concurrent background recapture (the live-mode tick this click arms)
      // is what captures it — not the foreground tab. See maybeReturnAfterCapture.
      const rv = String(msg.provider || msg.vendor || "").toLowerCase();
      try { chrome.storage.local.set({ so_return_tab: { tabId: sender.tab.id, windowId: sender.tab.windowId, ts: Date.now(), vendor: rv } }); } catch (_) { /* non-fatal */ }
    }
    if (!/^https:\/\//i.test(url)) {
      sendResponse({ ok: false, error: "invalid-url" });
      return; // sync response, no need to return true
    }
    // v1.4.1: wipe portal session cookies BEFORE opening the tab so an
    // operator who's already signed in as one client doesn't land in
    // that dashboard and re-scrape the same account. Each click = a
    // fresh, stateless visit to the portal login screen.
    (async () => {
      try {
        const domains = ["greenmountainpower.com", "smarthub.coop"];
        let host;
        try { host = new URL(url).hostname; } catch (_) { host = ""; }
        // Only wipe cookies for the portal we're opening — never touch
        // the user's other browsing.
        const matchDomain = domains.find((d) => host.endsWith(d));
        if (matchDomain) {
          const cookies = await chrome.cookies.getAll({ domain: matchDomain });
          await Promise.all(cookies.map((c) => {
            const protocol = c.secure ? "https://" : "http://";
            const cookieUrl = `${protocol}${c.domain.replace(/^\./, "")}${c.path}`;
            return chrome.cookies.remove({
              url: cookieUrl, name: c.name, storeId: c.storeId,
            });
          }));
        }
        // v1.8.0: SolarEdge is intentionally NOT in the wipe list — for a
        // single-owner connect we WANT their existing monitoring session to
        // ride (zero extra logins). Instead, arm the capture-intent flag so
        // solaredge_content.js knows this visit came from an explicit AO
        // "Connect SolarEdge" click and may read the durable key.
        if (host.endsWith("solaredge.com")) {
          try {
            await chrome.storage.local.set({
              so_capture_intent: { vendor: "solaredge", ts: Date.now() },
            });
          } catch (_) { /* non-fatal */ }
        }
        // v1.9.0: same for Fronius Solar.web — arm the fronius capture intent so
        // solarweb_content.js reads the owner's live readings on this explicit
        // "Connect Fronius" visit. Solar.web is NOT in the wipe list either
        // (we want their existing session to ride — zero extra logins).
        if (host.endsWith("solarweb.com")) {
          try {
            await chrome.storage.local.set({
              so_capture_intent: { vendor: "fronius", ts: Date.now() },
            });
          } catch (_) { /* non-fatal */ }
          try { if (typeof self.__soArmLive === "function") await self.__soArmLive("fronius"); } catch (_) { /* non-fatal */ }
        }
        // v1.9.2: same for SMA ennexOS (Sunny Portal). Not in the wipe list —
        // ride the owner's existing session.
        if (host.endsWith("sunnyportal.com")) {
          try {
            await chrome.storage.local.set({
              so_capture_intent: { vendor: "sma", ts: Date.now() },
            });
          } catch (_) { /* non-fatal */ }
          try { if (typeof self.__soArmLive === "function") await self.__soArmLive("sma"); } catch (_) { /* non-fatal */ }
        }
        // v1.9.12: Chint / CPS — the real owner portal is
        // monitor.chintpowersystems.com (HAR-grounded 2026-06-16); the older
        // solar.chintpower.com guess is kept for safety. Not in the wipe list —
        // ride the owner's existing monitoring session (zero extra logins).
        if (host.endsWith("chintpowersystems.com") || host.endsWith("chintpower.com")) {
          try {
            await chrome.storage.local.set({
              so_capture_intent: { vendor: "chint", ts: Date.now() },
            });
          } catch (_) { /* non-fatal */ }
          // v1.9.53: arm CHINT live-mode (a fast 4-min background recapture that keeps
          // live power fresh) on this explicit Connect-Chint click — the opt-in.
          try { if (typeof self.__soArmChintLive === "function") await self.__soArmChintLive(); } catch (_) { /* non-fatal */ }
        }
        // v1.9.23: GMP utility-meter PRODUCTION capture — arm the gmp intent so
        // gmp_meter_content.js reads the owner's solar generation on this explicit
        // "Connect GMP" visit. NOTE: greenmountainpower.com IS in the cookie-wipe
        // list above (each bill capture is a fresh stateless visit), so for a
        // meter-production connect the owner signs in again on this visit — that's
        // expected; the intent flag survives the wipe (it lives in extension
        // storage, not site cookies).
        if (host.endsWith("greenmountainpower.com")) {
          try {
            await chrome.storage.local.set({
              so_capture_intent: { vendor: "gmp", ts: Date.now() },
            });
          } catch (_) { /* non-fatal */ }
        }
        // v1.9.25: SmartHub co-op (VEC / WEC) utility-meter PRODUCTION capture —
        // arm the matching intent so smarthub_content.js forwards the owner's
        // short-lived SmartHub session for a server-side generation pull. The
        // vendor code mirrors the per-host provider (vec/wec); NOTE smarthub.coop
        // IS in the cookie-wipe list above, so each connect is a fresh sign-in —
        // the intent flag survives the wipe (extension storage, not site cookies).
        if (host.endsWith("smarthub.coop")) {
          const shVendor =
            host === "vermontelectric.smarthub.coop" ? "vec"
            : host === "washingtonelectric.smarthub.coop" ? "wec"
            : (msg.provider || msg.vendor || "").toLowerCase();
          if (shVendor === "vec" || shVendor === "wec") {
            try {
              await chrome.storage.local.set({
                so_capture_intent: { vendor: shVendor, ts: Date.now() },
              });
            } catch (_) { /* non-fatal */ }
          }
        }
      } catch (e) {
        // Cookie wipe is best-effort; opening the tab still proceeds.
        console.warn("[so] cookie wipe failed", e);
      }
      // Clear a BLOATED Fronius/SMA cookie blob before opening the portal so a manual
      // Connect / dashboard-sync click can't land on ERR_HTTP2_PROTOCOL_ERROR. No-op on a
      // healthy session (only fires >8KB) so we still ride the owner's existing login.
      try {
        const _pv = host.endsWith("solarweb.com") ? "fronius" : host.endsWith("sunnyportal.com") ? "sma" : null;
        if (_pv && typeof self.__soPruneCookies === "function") await self.__soPruneCookies(_pv);
      } catch (_) { /* non-fatal */ }
      chrome.tabs.create({ url, active }, (tab) => {
        if (chrome.runtime.lastError) {
          sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          // For a BACKGROUND (silent) Fronius/SMA capture tab — the dashboard "sync"
          // chip — register it so the nav-driven auto-login self-heals a lapsed session
          // (fills vault creds when the portal bounces to its SSO login). Foreground
          // tabs (onboarding/owner-present, possibly a NEW account) are left alone.
          try {
            if (!active && typeof self.__soRegisterAutoLoginTab === "function") {
              // DOT-ANCHORED allowlist (host is page-supplied via SO_OPEN_PORTAL): match
              // only the genuine vendor portals, never an attacker-registrable lookalike
              // like "evilsolarweb.com". Mirrors the manifest host_permissions entries.
              const v = (host === "www.solarweb.com" || host.endsWith(".solarweb.com")) ? "fronius"
                : (host === "ennexos.sunnyportal.com" || host.endsWith(".sunnyportal.com")) ? "sma"
                : null;
              if (v) self.__soRegisterAutoLoginTab(tab && tab.id, v);
            }
          } catch (_) { /* non-fatal */ }
          sendResponse({ ok: true, tabId: tab && tab.id });
        }
      });
    })();
    return true; // async sendResponse
  }
  // v1.4.3: page asks us to wipe cookies for a portal domain BEFORE
  // it calls window.open() directly. window.open() inside a user-
  // initiated click handler always foregrounds (no extension involved,
  // no popup blocker). We just handle the session-reset side here.
  //
  // v1.4.6: added timestamps for diagnosing wipe-vs-tab-load races,
  // and set so_pending_storage_wipe so portal_cleaner.js can clear
  // localStorage/sessionStorage on the first document_start of the
  // portal page.
  if (msg.type === "SO_WIPE_COOKIES") {
    const domain = String(msg.domain || "");
    const reqId = msg.reqId || "";
    const allowed = ["greenmountainpower.com", "smarthub.coop"];
    // Exact-or-subdomain match only. A bare endsWith() lets a look-alike apex
    // like "notgreenmountainpower.com" satisfy endsWith("greenmountainpower.com")
    // and wipe the victim's real utility cookies — require a dot boundary.
    const domainAllowed = allowed.some((d) => domain === d || domain.endsWith("." + d));
    if (!domainAllowed) {
      sendResponse({ ok: false, error: "domain-not-allowed" });
      return;
    }
    (async () => {
      const t0 = Date.now();
      console.log(`[SO ${t0}] wipe-start domain=${domain} reqId=${reqId}`);
      try {
        const cookies = await chrome.cookies.getAll({ domain });
        await Promise.all(cookies.map((c) => {
          const protocol = c.secure ? "https://" : "http://";
          const cookieUrl = `${protocol}${c.domain.replace(/^\./, "")}${c.path}`;
          return chrome.cookies.remove({
            url: cookieUrl, name: c.name, storeId: c.storeId,
          });
        }));
        const elapsed = Date.now() - t0;
        console.log(`[SO ${Date.now()}] wipe-done domain=${domain} wiped=${cookies.length} +${elapsed}ms`);
        // Signal portal_cleaner.js to clear localStorage/sessionStorage on next portal load.
        await chrome.storage.local.set({
          so_pending_storage_wipe: { domain, ts: t0 },
        });
        sendResponse({ ok: true, wiped: cookies.length });
      } catch (e) {
        console.warn(`[SO ${Date.now()}] wipe-error domain=${domain}:`, e);
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true;
  }

  // v1.3.0: SPA hands us a tenant key + endpoint (kills the copy-paste
  // activation-code step). We persist immediately and reply with the
  // resulting state so the SPA can show a "paired ✓" badge.
  if (msg.type === "SO_PAIR") {
    (async () => {
      try {
        const tenantKey = String(msg.tenantKey || "").trim();
        if (!tenantKey) {
          sendResponse({ ok: false, error: "missing-tenant-key" });
          return;
        }
        const update = { [STORAGE_KEYS.TENANT_KEY]: tenantKey };
        // SECURITY: only accept a KNOWN endpoint, never an arbitrary one — a page-supplied
        // endpoint could repoint sync to an attacker's server (defense-in-depth; the page
        // bridge no longer forwards `endpoint` at all).
        if (msg.endpoint && typeof msg.endpoint === "string" &&
            (msg.endpoint === PROD_ENDPOINT || msg.endpoint === FALLBACK_ENDPOINT)) {
          update[STORAGE_KEYS.ENDPOINT] = msg.endpoint;
        }
        await chrome.storage.local.set(update);
        const s = await chrome.storage.local.get([STORAGE_KEYS.LAST_SYNC]);
        const lastSyncAt = s[STORAGE_KEYS.LAST_SYNC]
          ? s[STORAGE_KEYS.LAST_SYNC].at || null
          : null;
        sendResponse({
          ok: true,
          version: chrome.runtime.getManifest().version,
          lastSyncAt,
        });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true;
  }
  // v1.3.0: SPA wants current extension state synchronously on mount.
  if (msg.type === "SO_STATUS_REQUEST") {
    (async () => {
      try {
        const s = await chrome.storage.local.get([
          STORAGE_KEYS.TENANT_KEY,
          STORAGE_KEYS.LAST_SYNC,
          STORAGE_KEYS.LAST_PAYLOAD,
          STORAGE_KEYS.LAST_LOGIN_STATE,
        ]);
        sendResponse({
          ok: true,
          version: chrome.runtime.getManifest().version,
          tenantKeySet: !!s[STORAGE_KEYS.TENANT_KEY],
          lastSyncAt: s[STORAGE_KEYS.LAST_SYNC]
            ? s[STORAGE_KEYS.LAST_SYNC].at || null
            : null,
          lastPayload: s[STORAGE_KEYS.LAST_PAYLOAD] || null,
          loginState: s[STORAGE_KEYS.LAST_LOGIN_STATE] || null,
        });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true;
  }
  // v1.3.0: content.js / vec_content.js classified the current utility
  // tab. Persist the latest per-provider state and rebroadcast to every
  // solaroperator.org tab so the onboarding wizard mirrors it live.
  if (msg.type === "LOGIN_STATE_DETECTED") {
    (async () => {
      try {
        const payload = {
          provider: msg.provider,
          state: msg.state,
          url: msg.url,
          at: msg.at || new Date().toISOString(),
        };
        const s = await chrome.storage.local.get([STORAGE_KEYS.LAST_LOGIN_STATE]);
        const merged = { ...(s[STORAGE_KEYS.LAST_LOGIN_STATE] || {}), [payload.provider]: payload };
        await chrome.storage.local.set({ [STORAGE_KEYS.LAST_LOGIN_STATE]: merged });
        broadcastToSoTabs({ type: "SO_LOGIN_STATE", ...payload });
        // v1.9.33: if a silent recapture is in flight for this vendor and its
        // session just lapsed, attempt client-side auto-login (opt-out + creds gate
        // are enforced inside). sender.tab.id is the recap's background tab.
        try {
          const tabId = sender && sender.tab && sender.tab.id;
          if (typeof self.__soRecapTryAutoLogin === "function") {
            self.__soRecapTryAutoLogin(payload.provider, tabId, payload.state);
          }
        } catch (_) {}
      } catch (e) {
        console.warn("[EnergyAgent] LOGIN_STATE_DETECTED handling failed:", e);
      }
    })();
    return false;
  }
});

// v1.9.33: vault message API for the popup UI — set/clear creds + toggle opt-out +
// status. Secrets only ever flow popup -> background (to be encrypted at rest); the
// status reply NEVER includes the actual password.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg.type !== "string" || !msg.type.startsWith("SO_VAULT_")) return;
  (async () => {
    try {
      if (typeof SoVault === "undefined") { sendResponse({ ok: false, error: "vault-unavailable" }); return; }
      if (msg.type === "SO_VAULT_STATUS") {
        sendResponse({ ok: true, status: await SoVault.status() });
      } else if (msg.type === "SO_VAULT_SET") {
        const ok = await SoVault.set(msg.vendor, msg.username, msg.password);
        if (ok) {
          // Saving a portal password is an explicit "I use this vendor" signal: clear
          // any auto-login pause so the new creds get a fresh try, and arm tight
          // live-mode so production refreshes every few minutes and the next silent
          // recapture self-heals a lapsed session with these creds. Fronius/SMA use the
          // tab-based armLive; Chint uses its minimized-popup walk (armChintLive). With
          // creds saved, Chint's walk now auto-logs in on a dead session too (v1.9.87) —
          // the login_required → recapTryAutoLogin → soFillLoginForm path fires on the
          // same-origin Chint login page, so Chint is hands-off like SMA/Fronius.
          try { if (typeof self.__soAutoLoginResetFails === "function") self.__soAutoLoginResetFails(msg.vendor); } catch (_) {}
          try { if (typeof self.__soKeepwarmResetFails === "function") self.__soKeepwarmResetFails(msg.vendor); } catch (_) {}
          try { if ((msg.vendor === "fronius" || msg.vendor === "sma") && typeof self.__soArmLive === "function") await self.__soArmLive(msg.vendor); } catch (_) {}
          try { if (msg.vendor === "chint" && typeof self.__soArmChintLive === "function") await self.__soArmChintLive(); } catch (_) {}
        }
        sendResponse({ ok });
      } else if (msg.type === "SO_VAULT_CLEAR") {
        await SoVault.clear(msg.vendor);
        sendResponse({ ok: true });
      } else if (msg.type === "SO_VAULT_OPTOUT") {
        await SoVault.setOptOut(msg.vendor, !!msg.optedOut);
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false, error: "unknown" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e && e.message || e) });
    }
  })();
  return true; // async sendResponse
});

// Heartbeat — ping the server every 60s so the onboarding screen can
// distinguish "extension active" from "not detected." Only fires when the
// tenant key is set and at least one GMP tab is open.
const HEARTBEAT_ENDPOINT_PATH = "/v1/extension/heartbeat";
chrome.alarms.create("heartbeat", { periodInMinutes: 1 });

async function sendHeartbeat() {
  const { tenantKey, endpoint } = await getSettings();
  if (!tenantKey) return;
  // Derive the base URL from the sync endpoint (same origin).
  const base = endpoint.replace(/\/v1\/sync$/, "");
  try {
    await fetch(`${base}${HEARTBEAT_ENDPOINT_PATH}`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${tenantKey}` },
    });
  } catch {
    // Non-fatal — heartbeat is best-effort.
  }
}

// ── GMP session keep-alive (v1.9.49) ─────────────────────────────────────────
// Server-side token refresh is DEAD: GMP's token endpoint 403s any headless
// (non-browser) call — "AUTHORIZATION_FAILURE" without an Origin, "Invalid CORS
// request" with one — so the backend cron can NEVER renew an operator's JWT
// (prod: lastRefresh=NEVER on every session). Every operator hit the ~21-day wall
// and got a "reconnect" email. The fix lives HERE, in the browser, where it works:
// when the JWT nears expiry we silently open greenmountainpower.com in a BACKGROUND
// tab (NO cookie wipe → rides the operator's persistent "remember me" session).
// GMP's own SPA restores + refreshes the token in-browser (cookie + CORS satisfied),
// content.js reads the fresh apitoken and re-POSTs it through the existing /v1/sync
// path — extending the session ~21 days with zero clicks, and keeping expires_at
// far enough out that the (broken) backend refresh never even runs. If the browser
// session has ALSO lapsed (capture can't ride it), we degrade to ONE gentle one-tap
// reconnect notification per day. Fail-safe: never worse than the old notify-only path.
const GMP_RECAP_AHEAD_DAYS = 8;          // begin silent refresh when < 8 days to expiry
const GMP_RECAP_BUDGET_MS = 80 * 1000;   // background-tab lifetime: SPA load + refresh + capture
const GMP_RECAP_ATTEMPT_KEY = "so_gmp_recap_attempt";  // YYYY-MM-DD — max 1 silent attempt/day
const GMP_NUDGE_KEY = "so_gmp_nudge";                  // YYYY-MM-DD — max 1 notification/day

// Open GMP in a background tab and let content.js capture the refreshed token.
// Resolves true iff a fresher token actually landed (captured expiry advanced).
async function silentGmpRecapture(prevExpiresMs) {
  return new Promise((resolve) => {
    let settled = false;
    chrome.tabs.create({ url: "https://greenmountainpower.com/", active: false }, (tab) => {
      if (chrome.runtime.lastError || !tab) { resolve(false); return; }
      const tabId = tab.id;
      setTimeout(async () => {
        if (settled) return; settled = true;
        try { chrome.tabs.remove(tabId, () => void chrome.runtime.lastError); } catch (_) {}
        let newExp = 0;
        try {
          const s2 = await chrome.storage.local.get(STORAGE_KEYS.LAST_PAYLOAD);
          const te = s2[STORAGE_KEYS.LAST_PAYLOAD] && s2[STORAGE_KEYS.LAST_PAYLOAD].tokenExpires;
          newExp = te ? new Date(te).getTime() : 0;
        } catch (_) {}
        resolve(newExp > prevExpiresMs + 60 * 1000);   // a fresh token landed
      }, GMP_RECAP_BUDGET_MS);
    });
  });
}

async function gmpKeepAlive() {
  const s = await chrome.storage.local.get(STORAGE_KEYS.LAST_PAYLOAD);
  const last = s[STORAGE_KEYS.LAST_PAYLOAD];
  if (!last || !last.tokenExpires) return;                 // no GMP session known yet
  if ((last.provider || "gmp") !== "gmp") return;          // most-recent capture wasn't GMP
  const expiresMs = new Date(last.tokenExpires).getTime();
  const daysLeft = (expiresMs - Date.now()) / (1000 * 60 * 60 * 24);
  if (daysLeft <= 0 || daysLeft >= GMP_RECAP_AHEAD_DAYS) return;  // healthy, or already gone

  const today = new Date().toISOString().slice(0, 10);
  // One silent refresh attempt per day (avoids opening a tab on every 12h tick).
  const a = await chrome.storage.local.get(GMP_RECAP_ATTEMPT_KEY);
  if (a[GMP_RECAP_ATTEMPT_KEY] !== today) {
    await chrome.storage.local.set({ [GMP_RECAP_ATTEMPT_KEY]: today });
    const ok = await silentGmpRecapture(expiresMs);
    if (ok) { console.log("[EnergyAgent] GMP session silently refreshed in-browser"); return; }
  }
  // Silent refresh unavailable (browser GMP session also lapsed) — gentle one-tap
  // nudge, max once/day, only when genuinely close to expiry.
  if (daysLeft < 3) {
    const n = await chrome.storage.local.get(GMP_NUDGE_KEY);
    if (n[GMP_NUDGE_KEY] !== today) {
      await chrome.storage.local.set({ [GMP_NUDGE_KEY]: today });
      chrome.notifications.create(`gmp-reconnect-${today}`, {
        type: "basic",
        iconUrl: "icons/icon128.png",
        title: "EnergyAgent: reconnect needed",
        message: `Your GMP session expires in ${Math.ceil(daysLeft)} day(s). Click to open greenmountainpower.com — one sign-in keeps your bill pulls running.`,
        requireInteraction: true,
      });
    }
  }
}

// One-tap recovery: clicking the GMP nudge opens the portal as an ACTIVE tab; the
// operator signs in once, content.js rides the fresh session, and we're silent again.
chrome.notifications.onClicked.addListener((notifId) => {
  if (!/^gmp-reconnect-/.test(String(notifId || ""))) return;
  chrome.tabs.create({ url: "https://greenmountainpower.com/account/login/", active: true },
    () => void chrome.runtime.lastError);
  try { chrome.notifications.clear(notifId, () => void chrome.runtime.lastError); } catch (_) {}
});

// Expiry check (every 12h): silently refresh the GMP JWT in-browser before it runs
// out; only notify if the browser session has also lapsed.
chrome.alarms.create("token-expiry-check", { periodInMinutes: 60 * 12 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "heartbeat") { await sendHeartbeat(); return; }
  if (alarm.name !== "token-expiry-check") return;
  try { await gmpKeepAlive(); } catch (e) { console.warn("[EnergyAgent] gmpKeepAlive failed", e); }
});

chrome.runtime.onInstalled.addListener(async (details) => {
  chrome.action.setBadgeText({ text: "" });

  // v1.0.2 migration: existing installs have api_endpoint stuck on the
  // Railway URL. Clear it so they pick up the new PROD_ENDPOINT
  // (api.solaroperator.org). User-customized endpoints (anything else) are
  // left alone.
  if (details.reason === "update") {
    const s = await chrome.storage.local.get(STORAGE_KEYS.ENDPOINT);
    if (s[STORAGE_KEYS.ENDPOINT] === FALLBACK_ENDPOINT) {
      await chrome.storage.local.remove(STORAGE_KEYS.ENDPOINT);
      console.log("[EnergyAgent] v1.0.2 migration: cleared stale Railway endpoint, now defaulting to", PROD_ENDPOINT);
    }
  }

  // v1.9.86: the old v1.9.63 "chint live-mode removed" stub used to run HERE on every
  // install/update and force `so_chint_live = {on:false}` + clear the alarm — which
  // silently RE-DISABLED Chint background refresh on EVERY version bump after v1.9.81
  // re-enabled it (autoArmKnownLive then saw on:false and skipped Chint). Removed. Chint
  // background refresh now survives updates via reArmLive/autoArmKnownLive + the one-time
  // migrateChintBackgroundOnce. We still reap any recapture tab orphaned by an MV3 worker
  // termination so they don't pile up (Ford: "came back to a bunch of chint tabs").
  try { if (typeof self.__soReapOrphanRecapture === "function") await self.__soReapOrphanRecapture(); } catch (_) {}

  // v1.5.2: retro-inject so_bridge.js into any SO tabs the user already had
  // open at install/update time. Content scripts declared in manifest only
  // fire on future navigations, so without this the onboarding page sits
  // there waiting for SO_EXTENSION_PRESENT forever (user has to refresh).
  try {
    chrome.tabs.query({ url: SO_TAB_URLS }, (tabs) => {
      if (chrome.runtime.lastError) { void chrome.runtime.lastError; return; }
      for (const t of tabs || []) {
        if (typeof t.id !== "number") continue;
        chrome.scripting.executeScript({
          target: { tabId: t.id },
          files: ["so_bridge.js"],
        }).catch((e) => {
          // Tab may be in a state that doesn't accept injection (e.g. chrome://
          // redirect) — non-fatal, swallow.
          console.warn("[EnergyAgent] so_bridge inject skipped for tab", t.id, e && e.message);
        });
      }
    });
  } catch (e) {
    console.warn("[EnergyAgent] retro-inject failed:", e);
  }
});

// ============================================================================
// v1.9.30: SILENT AUTO-RECAPTURE for inverter live power (Fronius / SMA / Chint)
// ----------------------------------------------------------------------------
// Why: unlike SolarEdge (polled live by the backend on every dashboard load),
// the extension-captured vendors only refresh their live AC power when a capture
// runs — so the production bar would freeze at the last manual capture. This makes
// it SLICK: on an hourly timer (while Chrome is open) we silently open each portal in
// a BACKGROUND (inactive) tab, let the existing content script ride the owner's
// logged-in session and grab fresh power, POST it straight to the backend with the
// stored tenant key (the /inverter-capture endpoint is dual-auth: session OR key),
// then auto-close the tab. The owner sees nothing. If a portal session has expired
// (capture can't ride it), we degrade to ONE gentle "reconnect" notification per
// vendor per day instead of nagging. Reuses the existing intent + *_CAPTURED wiring.
// ============================================================================
(() => {
  const RECAP_VENDORS = {
    fronius: "https://www.solarweb.com/",
    sma: "https://ennexos.sunnyportal.com/",
    chint: "https://monitor.chintpowersystems.com/",
  };
  // v1.9.97 — UTILITY portals (monthly BILLS, not live power). Kept in a SEPARATE
  // map so the inverter hourly cycle / live loop / tab-reaper never iterate or
  // hammer a utility (those use RECAP_VENDORS only). The background open + capture
  // reuse the SAME machinery (recaptureVendor → so_capture_intent → the content
  // script POSTs the bill), but on a DAILY cadence (see UTIL_LIVE_PERIOD_MIN), so
  // we re-open a utility portal at most a couple times a day, never every 6 min.
  // GMP is one host; SmartHub co-ops each have their own *.smarthub.coop host,
  // resolved from the registry by code so the URL matches the saved credential.
  const RECAP_UTILITIES = {
    gmp: "https://greenmountainpower.com/",
    // Two grounded co-ops are seeded for clarity; ANY connected co-op is added at
    // arm time via _utilityPortalUrl(code) from the registry (so vec/wec/sh_* all
    // work without listing them here). These literals are just a fast path.
    vec: "https://vermontelectric.smarthub.coop/",
    wec: "https://washingtonelectric.smarthub.coop/",
  };
  // Resolve a utility code to its portal URL: GMP, a seeded co-op, or — for any
  // other connected co-op — its *.smarthub.coop host from the registry.
  function _utilityPortalUrl(code) {
    const c = String(code || "").toLowerCase();
    if (RECAP_UTILITIES[c]) return RECAP_UTILITIES[c];
    try {
      const reg = self.SMARTHUB_REGISTRY;
      if (reg) { for (const host of Object.keys(reg)) { if (reg[host] && reg[host].provider === c) return "https://" + host + "/"; } }
    } catch (_) {}
    if (c.startsWith("sh_")) return null;   // discovered co-op with no known host yet — can't background-open
    return null;
  }
  // Portal URL for ANY code we background-open: inverter vendor OR utility.
  function _recapUrlFor(code) {
    return RECAP_VENDORS[code] || _utilityPortalUrl(code) || null;
  }
  const RECAP_ALARM = "inverter-recapture";
  const RECAP_PERIOD_MIN = 60;           // hourly while the browser is running
  const TAB_BUDGET_MS = 150 * 1000;      // up to 2.5min — room for an auto-login + re-poll + capture
  const NUDGE_KEY = "so_recap_nudges";   // { fronius:"YYYY-MM-DD", ... } 1 nudge/vendor/day
  const STATE_KEY = "so_recap_state";    // { running, vendor, tabId, startedAt }
  const LAST_KEY = "so_recap_last";      // { fronius:{at,ok,sites}, ... } diagnostics

  function rlog(...a) { try { console.log("[EnergyAgent/recap]", ...a); } catch (_) {} }

  // SYNCHRONOUS in-service-worker single-flight. The persisted so_recap_state lock is
  // set several awaits AFTER the busy-check (inside recaptureVendor's tab-create
  // callback), so two alarms firing in the SAME service-worker wake could both pass
  // the check and open two background portal tabs (TOCTOU). This module-level flag is
  // read+set with NO await between, so the second tick in the same wake sees it set and
  // bails. so_recap_state stays as the durable/watchdog + cross-SW-restart backstop.
  let _liveBusy = false;
  // ── AUTO-LOGIN state (navigation-driven, v1.9.71) ──────────────────────────
  // The portals redirect a DEAD session to a separate SSO origin with NO content
  // script (Fronius -> login.online.fronius.com / WSO2; SMA -> login.sma.energy /
  // Keycloak), so the old "content script broadcasts login_required" trigger could
  // only ever run soFillLoginForm on the dashboard page (which has no password
  // field). We now ALSO watch our OWN capture tabs via chrome.tabs.onUpdated and
  // fill the form the instant such a tab lands on a known SSO login origin.
  const _autoLoginSubmittedTab = new Set();   // tabIds we already submitted creds on (never resubmit the same tab → no lockout loop)
  const _autoLoginAttemptsTab = new Map();    // tabId -> attempts (form-not-ready retries)
  const _openPortalTabs = new Map();          // tabId -> vendor, for BACKGROUND SO_OPEN_PORTAL capture tabs (the dashboard "sync" chip) so the nav auto-login fires for them too
  // vendor -> consecutive submits that did NOT yield a capture (wrong pw / changed form).
  // PERSISTED in chrome.storage (NOT in-memory): the MV3 service worker is torn down
  // between the 6-min live ticks, so an in-memory counter would reset to 0 every tick and
  // the pause would never trigger — letting a wrong password resubmit forever and lock the
  // owner's portal account. Persisting it is what actually makes the lockout guard work.
  const AUTOLOGIN_FAILS_KEY = "so_autologin_fails";   // { fronius: n, sma: n }
  async function autoLoginFailsGet(vendor) {
    try { const s = await chrome.storage.local.get(AUTOLOGIN_FAILS_KEY); return Number((s[AUTOLOGIN_FAILS_KEY] || {})[vendor]) || 0; } catch (_) { return 0; }
  }
  async function autoLoginFailsSet(vendor, n) {
    try {
      const s = await chrome.storage.local.get(AUTOLOGIN_FAILS_KEY);
      const m = s[AUTOLOGIN_FAILS_KEY] || {};
      if (!n || n <= 0) delete m[vendor]; else m[vendor] = n;
      await chrome.storage.local.set({ [AUTOLOGIN_FAILS_KEY]: m });
    } catch (_) {}
  }
  const AUTOLOGIN_MAX_TAB_ATTEMPTS = 5;
  const AUTOLOGIN_MAX_VENDOR_FAILS = 3;
  // vendor -> consecutive keep-warm refreshes that captured NOTHING (dead session we can't
  // auto-login — e.g. used vendor whose creds were never saved). Persisted (same reason as
  // the auto-login counter) so keep-warm GIVES UP after a few futile tries instead of opening
  // a background tab every cycle forever. Reset to 0 on ANY successful capture or a creds save.
  const KEEPWARM_FAILS_KEY = "so_keepwarm_fails";
  const KEEPWARM_MAX_FAILS = 4;
  async function keepwarmFailsGet(vendor) {
    try { const s = await chrome.storage.local.get(KEEPWARM_FAILS_KEY); return Number((s[KEEPWARM_FAILS_KEY] || {})[vendor]) || 0; } catch (_) { return 0; }
  }
  async function keepwarmFailsSet(vendor, n) {
    try {
      const s = await chrome.storage.local.get(KEEPWARM_FAILS_KEY);
      const m = s[KEEPWARM_FAILS_KEY] || {};
      if (!n || n <= 0) delete m[vendor]; else m[vendor] = n;
      await chrome.storage.local.set({ [KEEPWARM_FAILS_KEY]: m });
    } catch (_) {}
  }
  // Distinct per-vendor phase + jitter so the live alarms don't march in lockstep
  // (avoids same-wake collisions) and a fleet-wide Web Store update doesn't synchronize
  // every browser's first tick onto the single web replica.
  const LIVE_STAGGER = { fronius: 1, sma: 2, chint: 3 };
  function _liveDelayMin(vendor) { return (LIVE_STAGGER[vendor] || 1) + Math.random() * 3; }

  // ── CHINT live mode (v1.9.53; background loop RE-ENABLED v1.9.81) ─────────────
  // A periodic refresh so Chint power/per-inverter data tracks the portal instead of
  // freezing at the last manual capture. Each tick runs the v1.9.77 programmatic site
  // walk in a MINIMIZED, UNFOCUSED popup (recaptureVendor newWindow:true) — proven
  // hands-off, no focus steal, no tab in the owner's strip. Armed by an AO "Connect
  // Chint" click / the portal-open hook / the live toggle, AND auto-armed for owners
  // who already use Chint (autoArmKnownLive + the v1.9.81 one-time migration), surviving
  // restarts/updates. Reuses recaptureVendor / recapPost / recapFinish + the
  // so_recap_state single-flight + _liveBusy, so it can never race the hourly cycle or
  // pile up popups. Degrades to an honest reconnect nudge after CHINT_LIVE_MAX_FAILS dead
  // cycles (lapsed session — Chint has no silent re-login) — never silent staleness.
  // (Disabled v1.9.63→v1.9.80 because pre-walk Chint needed a manual site click; obsolete.)
  const CHINT_LIVE_ALARM = "chint-live";
  const CHINT_LIVE_PERIOD_MIN = 10;         // background walk-refresh cadence; ~1.5x margin inside the backend's 15-min fresh window. Calmer than the old 4-min so the minimized popup blinks ~6x/hr, not 15x.
  const CHINT_LIVE_KEY = "so_chint_live";   // { on, armedAt, lastOkAt, fails }
  const CHINT_LIVE_MAX_FAILS = 3;           // ~30 min of dead cycles (lapsed session — Chint has no silent re-login) → disable + nudge, so a dead session stops churning popups
  async function chintLiveGet() { const s = await chrome.storage.local.get(CHINT_LIVE_KEY); return s[CHINT_LIVE_KEY] || null; }
  async function chintLiveSet(v) { try { await chrome.storage.local.set({ [CHINT_LIVE_KEY]: v }); } catch (_) {} }
  async function armChintLive() {
    // RE-ENABLED (v1.9.81): the v1.9.63 "Chint can't be captured silently — needs a site
    // click" premise is OBSOLETE. v1.9.77's programmatic per-site route walk fires
    // busTypeDevices with NO click, and v1.9.80 runs that walk in a MINIMIZED, UNFOCUSED
    // popup window that can't steal focus or add a tab to the owner's window (Ford verified
    // Sync-all Chint is fully hands-off). So Chint now gets the same background keep-fresh
    // loop as Fronius/SMA — runChintLiveTick → recaptureVendor("chint",{newWindow:true}).
    // Guards against the old tab-pileup the v1.9.63 note warned of: the _liveBusy
    // single-flight serializes all background surfaces, the 1-min recap-reaper closes any
    // orphan, recapFinish removes the popup the instant capture lands, and CHINT_LIVE_MAX_FAILS
    // disarms+nudges a dead session instead of churning popups forever.
    await chintLiveSet({ on: true, armedAt: Date.now(), lastOkAt: 0, fails: 0 });
    try { chrome.alarms.create(CHINT_LIVE_ALARM, { periodInMinutes: CHINT_LIVE_PERIOD_MIN, delayInMinutes: _liveDelayMin("chint") }); } catch (_) {}
    rlog("chint live-mode ARMED — background refresh every", CHINT_LIVE_PERIOD_MIN, "min via a minimized-popup site walk");
  }
  async function disarmChintLive(reason) {
    const v = (await chintLiveGet()) || {};
    await chintLiveSet({ ...v, on: false });
    try { chrome.alarms.clear(CHINT_LIVE_ALARM, () => void chrome.runtime.lastError); } catch (_) {}
    rlog("chint live-mode DISARMED:", reason || "");
  }
  self.__soArmChintLive = armChintLive;       // called by the OPEN_UTILITY_PORTAL chint branch
  self.__soDisarmChintLive = disarmChintLive;
  // One fast tick: open ONE invisible background Chint tab via the shared recapture
  // path (arms intent, POSTs via recapPost, closes the tab), then update the fail
  // counter from the recorded outcome (so_recap_last).
  async function runChintLiveTick() {
    if (_liveBusy) { rlog("chint-live: recapture in flight (sync lock) — skip"); return; }
    _liveBusy = true;
    try {
      const live = await chintLiveGet();
      if (!live || !live.on) { try { chrome.alarms.clear(CHINT_LIVE_ALARM, () => void chrome.runtime.lastError); } catch (_) {} return; }  // self-heal zombie alarm
      const { tenantKey } = await recapSettings();
      if (!tenantKey) { rlog("chint-live: no tenant key — skip"); return; }
      const st = await recapGetState();
      if (st && st.running && (Date.now() - (st.startedAt || 0)) < TAB_BUDGET_MS) { rlog("chint-live: recap busy — skip tick"); return; }
      const before = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {}).chint || {};
      // MINIMIZED, UNFOCUSED popup window (newWindow) — NOT a background tab. Chint's SPA
      // focuses its OWN tab on the walk's route changes, so a background tab gets pulled to
      // the foreground ("took me to the Chint tab"); a minimized popup can't be. recapFinish
      // removes the whole window the instant the walk's capture lands (or the watchdog fires).
      // 120s budget for the multi-site walk headroom (same as Sync-all's Chint surface).
      await recaptureVendor("chint", { newWindow: true, budgetMs: 120 * 1000 });
      const after = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {}).chint || {};
      const ok = !!(after.ok && after.at && after.at !== before.at);
      const cur = (await chintLiveGet()) || { on: true, fails: 0 };
      if (!cur.on) return;                       // disarmed mid-tick
      if (ok) { await chintLiveSet({ ...cur, fails: 0, lastOkAt: Date.now() }); return; }
      const fails = (cur.fails || 0) + 1;
      await chintLiveSet({ ...cur, fails });
      if (fails >= CHINT_LIVE_MAX_FAILS) {
        rlog("chint-live:", fails, "dead cycles — disabling + nudging");
        await disarmChintLive("max-fails");
        await recapMaybeNudge("chint");          // honest reconnect, not silent stale
      }
    } finally { _liveBusy = false; }
  }
  // -- Fronius / SMA live mode (generalized from Chint's) ----------------------
  // Same fast-refresh pattern as Chint so Fronius/SMA live power tracks the portal
  // instead of freezing between hourly recaptures. 6-min cadence: ~2.5x margin inside
  // the backend's 15-min live window — visually identical freshness to 4-min, but the
  // single-flight lock serializes ALL vendors' background tabs, so 6 (vs 4) roughly
  // halves the per-vendor tab-open rate and keeps it AMBIENT rather than an
  // effectively-always-open background tab (the adversarial-review verdict). Chint
  // stays at 4 (already shipped). Degrades to a one/day reconnect nudge after repeated
  // dead cycles -- never silent staleness. Reuses recaptureVendor / recapPost /
  // recapFinish + the so_recap_state single-flight + the _liveBusy sync lock.
  const LIVE_PERIOD_MIN = { fronius: 6, sma: 6 };
  const LIVE_MAX_FAILS = 6;
  const _liveKey = (v) => "so_live_" + v;
  async function liveGet(v) { const k = _liveKey(v); const s = await chrome.storage.local.get(k); return s[k] || null; }
  async function liveSet(v, val) { try { await chrome.storage.local.set({ [_liveKey(v)]: val }); } catch (_) {} }
  async function armLive(vendor) {
    if (!RECAP_VENDORS[vendor] || !LIVE_PERIOD_MIN[vendor]) return;
    const per = LIVE_PERIOD_MIN[vendor];
    await liveSet(vendor, { on: true, armedAt: Date.now(), lastOkAt: 0, fails: 0 });
    try { chrome.alarms.create("live-" + vendor, { periodInMinutes: per, delayInMinutes: _liveDelayMin(vendor) }); } catch (_) {}
    rlog(vendor, "live-mode ARMED -- refresh every", per, "min via a background tab");
  }

  // ── UTILITY background refresh (v1.9.97) ────────────────────────────────────
  // Utilities pull monthly BILLS, not live power, so they get a DAILY-ish cadence
  // — NEVER the 6-min live loop (that would hammer a utility portal for data that
  // changes once a month). Each tick opens the utility portal in a MINIMIZED,
  // UNFOCUSED popup (the same hands-off surface Chint uses) so the existing
  // content script (gmp_meter_content / smarthub_content) re-captures the latest
  // bill and, on a lapsed session, the nav/secondary auto-login trigger signs the
  // owner back in with their saved utility credential. Per-code state + alarm so
  // an owner with GMP + a co-op refreshes each independently. Same single-flight
  // (_liveBusy) + recap-state lock + recapFinish teardown as every other surface,
  // so it can never pile up tabs or race the inverter loops. Degrades to one
  // honest reconnect nudge/day after UTIL_LIVE_MAX_FAILS dead cycles.
  const UTIL_LIVE_PERIOD_MIN = 720;          // twice a day (~every 12h). Bills land monthly; this just keeps a fresh one current + the session warm. NEVER the 6-min live cadence.
  const UTIL_LIVE_MAX_FAILS = 3;             // ~1.5 days of dead cycles → disarm + one nudge (lapsed session we couldn't auto-login)
  const UTIL_LIVE_ALARM_PREFIX = "util-live-";
  const _utilLiveKey = (c) => "so_util_live_" + c;
  const _utilLiveAlarm = (c) => UTIL_LIVE_ALARM_PREFIX + c;
  async function utilLiveGet(c) { const k = _utilLiveKey(c); const s = await chrome.storage.local.get(k); return s[k] || null; }
  async function utilLiveSet(c, val) { try { await chrome.storage.local.set({ [_utilLiveKey(c)]: val }); } catch (_) {} }
  function _utilDelayMin() { return 5 + Math.random() * 30; }   // first tick soon-ish but jittered, never a thundering herd on a utility
  async function armUtilityLive(code) {
    code = String(code || "").toLowerCase();
    if (!_isUtilityCode(code)) return;
    if (!_utilityPortalUrl(code)) { rlog("util-live: no portal URL for", code, "— can't background-open (discovered co-op w/o known host)"); return; }
    await utilLiveSet(code, { on: true, armedAt: Date.now(), lastOkAt: 0, fails: 0 });
    try { chrome.alarms.create(_utilLiveAlarm(code), { periodInMinutes: UTIL_LIVE_PERIOD_MIN, delayInMinutes: _utilDelayMin() }); } catch (_) {}
    rlog(code, "utility background refresh ARMED — every", UTIL_LIVE_PERIOD_MIN, "min (daily-ish; bills are monthly)");
  }
  async function disarmUtilityLive(code, reason) {
    const v = (await utilLiveGet(code)) || {};
    await utilLiveSet(code, { ...v, on: false });
    try { chrome.alarms.clear(_utilLiveAlarm(code), () => void chrome.runtime.lastError); } catch (_) {}
    rlog(code, "utility background refresh DISARMED:", reason || "");
  }
  async function reArmUtilityLive(code) {
    const cur = (await utilLiveGet(code)) || {};
    if (!_utilityPortalUrl(code)) return;
    await utilLiveSet(code, { on: true, armedAt: cur.armedAt || Date.now(), lastOkAt: cur.lastOkAt || 0, fails: cur.fails || 0 });
    try { chrome.alarms.create(_utilLiveAlarm(code), { periodInMinutes: UTIL_LIVE_PERIOD_MIN, delayInMinutes: _utilDelayMin() }); } catch (_) {}
    rlog(code, "utility background refresh RE-ARMED (fails preserved)");
  }
  self.__soArmUtilityLive = armUtilityLive;
  self.__soDisarmUtilityLive = disarmUtilityLive;
  // One utility refresh tick: open the portal in a minimized popup via the shared
  // recapture path, then update the fail counter from the recorded outcome.
  async function runUtilityLiveTick(code) {
    if (_liveBusy) { rlog("util-live:", code, "engine busy (sync lock) — skip"); return; }
    _liveBusy = true;
    try {
      const live = await utilLiveGet(code);
      if (!live || !live.on) { try { chrome.alarms.clear(_utilLiveAlarm(code), () => void chrome.runtime.lastError); } catch (_) {} return; }  // self-heal zombie alarm
      if ((await autoLoginFailsGet(code)) >= AUTOLOGIN_MAX_VENDOR_FAILS) { rlog("util-live:", code, "auto-login paused (bad creds) — skip"); return; }
      const { tenantKey } = await recapSettings();
      if (!tenantKey) { rlog("util-live: no tenant key — skip"); return; }
      const st = await recapGetState();
      if (st && st.running && (Date.now() - (st.startedAt || 0)) < TAB_BUDGET_MS) { rlog("util-live: recap busy — skip tick"); return; }
      const before = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {})[code] || {};
      await recaptureVendor(code, { newWindow: true, budgetMs: 150 * 1000 });   // headroom for a full re-login + bill capture
      const after = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {})[code] || {};
      const ok = !!(after.ok && after.at && after.at !== before.at);
      const cur = (await utilLiveGet(code)) || { on: true, fails: 0 };
      if (!cur.on) return;
      if (ok) { await utilLiveSet(code, { ...cur, fails: 0, lastOkAt: Date.now() }); return; }
      const fails = (cur.fails || 0) + 1;
      await utilLiveSet(code, { ...cur, fails });
      if (fails >= UTIL_LIVE_MAX_FAILS) {
        rlog("util-live:", code, fails, "dead cycles — disabling + nudging");
        await disarmUtilityLive(code, "max-fails");
        await recapMaybeNudge(code);
      }
    } finally { _liveBusy = false; }
  }
  // Re-arm an ALREADY-ON vendor's alarm (after a browser restart / extension update)
  // WITHOUT resetting the fail counter — so a vendor that legitimately self-disarmed
  // after MAX_FAILS dead cycles is NOT resurrected, and a still-healthy vendor's
  // disarm-on-lapse loop is preserved. Only an explicit owner "Connect" (armLive /
  // armChintLive) zeroes fails. Idempotent: chrome.alarms.create replaces same-named.
  async function reArmLive(vendor) {
    if (vendor === "chint") {
      // Re-arm chint's background walk-loop after a browser restart / extension update,
      // WITHOUT resetting fails (a Chint that self-disarmed after MAX_FAILS stays off until
      // the owner reopens the portal; autoArmKnownLive only reaches here when on !== false).
      // v1.9.81 — see armChintLive (re-enabled).
      const cur = (await chintLiveGet()) || {};
      await chintLiveSet({ on: true, armedAt: cur.armedAt || Date.now(), lastOkAt: cur.lastOkAt || 0, fails: cur.fails || 0 });
      try { chrome.alarms.create(CHINT_LIVE_ALARM, { periodInMinutes: CHINT_LIVE_PERIOD_MIN, delayInMinutes: _liveDelayMin("chint") }); } catch (_) {}
    } else {
      if (!LIVE_PERIOD_MIN[vendor]) return;
      const cur = (await liveGet(vendor)) || {};
      await liveSet(vendor, { on: true, armedAt: cur.armedAt || Date.now(), lastOkAt: cur.lastOkAt || 0, fails: cur.fails || 0 });
      try { chrome.alarms.create("live-" + vendor, { periodInMinutes: LIVE_PERIOD_MIN[vendor], delayInMinutes: _liveDelayMin(vendor) }); } catch (_) {}
    }
    rlog(vendor, "live-mode RE-ARMED (fails preserved)");
  }
  // On install/update/startup, turn live-mode ON for the extension-captured vendors the
  // owner actually USES — so near-live "just works" without a manual reconnect and
  // survives version updates / browser restarts. Guards (the adversarial-review fixes):
  //   • tenant key present (don't burn dead cycles on a logged-out browser)
  //   • vendor has a SUCCESSFUL capture (so_recap_last[v].ok) — NOT mere presence, since
  //     the first hourly cycle probes all 3 and writes failed entries for ones the owner
  //     never connected (which would otherwise spawn spurious reconnect nudges)
  //   • the vendor has NOT self-disarmed / been toggled off (live.on === false) — respect
  //     the sticky off until the human reconnects
  async function autoArmKnownLive() {
    const { tenantKey } = await recapSettings();
    if (!tenantKey) { rlog("auto-arm: no tenant key — skip"); return; }
    const known = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY]) || {};
    for (const v of Object.keys(RECAP_VENDORS)) {
      if (!(known[v] && known[v].ok)) continue;                 // owner doesn't (successfully) use it
      const live = (v === "chint") ? await chintLiveGet() : await liveGet(v);
      if (live && live.on === false) continue;                  // self-disarmed / toggled off — leave off
      await reArmLive(v);
    }
    // v1.9.97 — re-arm known UTILITY connections (daily cadence). "Known" = the
    // owner saved a utility credential (the explicit "I use this utility" signal,
    // since a utility bill capture may not have written so_recap_last yet). Respect
    // a sticky off (self-disarmed after MAX_FAILS or toggled off) and skip a
    // discovered co-op we have no portal URL for. Belt-and-suspenders: also re-arm
    // any utility that already produced a successful capture.
    try {
      const codes = new Set();
      if (typeof SoVault !== "undefined") {
        try {
          const st = await SoVault.status();
          for (const c of Object.keys(st || {})) { if (st[c] && st[c].hasCreds && _isUtilityCode(c)) codes.add(c); }
        } catch (_) {}
      }
      for (const c of Object.keys(known)) { if (known[c] && known[c].ok && _isUtilityCode(c)) codes.add(c); }
      for (const c of codes) {
        if (!_utilityPortalUrl(c)) continue;
        const live = await utilLiveGet(c);
        if (live && live.on === false) continue;                // sticky off — leave it
        await reArmUtilityLive(c);
      }
    } catch (e) { rlog("auto-arm utilities failed", e && e.message || e); }
  }
  self.__soAutoArmKnownLive = autoArmKnownLive;
  // One-time migration: Chint background refresh was hard-disabled before v1.9.81, so every
  // install carried so_chint_live = {on:false} from the old stub — which autoArmKnownLive
  // correctly treats as "owner left it off, leave it". v1.9.81 armed it once, BUT the leftover
  // onInstalled disarm stub (removed in v1.9.86) re-forced on:false on every later update, so
  // by v1.9.85 Chint was off again on every install. Bumping the flag re-arms it ONCE more for
  // owners who actually use Chint (a prior successful capture); with the disarm stub gone it now
  // STICKS. After this, a real disarm (MAX_FAILS / the UI toggle) sticks normally.
  async function migrateChintBackgroundOnce() {
    const FLAG = "so_chint_bg_migrated_v1986";
    try {
      const s = await chrome.storage.local.get([FLAG, LAST_KEY]);
      if (s[FLAG]) return;
      await chrome.storage.local.set({ [FLAG]: true });
      const known = s[LAST_KEY] || {};
      if (known.chint && known.chint.ok) {
        await armChintLive();
        rlog("chint background refresh: enabled for this known-Chint install (v1.9.81 one-time migration)");
      }
    } catch (_) {}
  }
  async function disarmLive(vendor, reason) {
    const v = (await liveGet(vendor)) || {};
    await liveSet(vendor, { ...v, on: false });
    try { chrome.alarms.clear("live-" + vendor, () => void chrome.runtime.lastError); } catch (_) {}
    rlog(vendor, "live-mode DISARMED:", reason || "");
  }
  self.__soArmLive = armLive;     // fronius/sma connect-arm calls this
  self.__soDisarmLive = disarmLive;
  async function runLiveTick(vendor) {
    if (_liveBusy) { rlog(vendor + "-live: recapture in flight (sync lock) -- skip"); return; }
    _liveBusy = true;
    try {
      const live = await liveGet(vendor);
      if (!live || !live.on) { try { chrome.alarms.clear("live-" + vendor, () => void chrome.runtime.lastError); } catch (_) {} return; }
      const { tenantKey } = await recapSettings();
      if (!tenantKey) { rlog(vendor + "-live: no tenant key -- skip"); return; }
      const st = await recapGetState();
      if (st && st.running && (Date.now() - (st.startedAt || 0)) < TAB_BUDGET_MS) { rlog(vendor + "-live: recap busy -- skip tick"); return; }
      const before = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {})[vendor] || {};
      await recaptureVendor(vendor, { budgetMs: 150 * 1000 });   // headroom for a full SSO re-login + redirect + sequential recapture on a lapsed session
      const after = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {})[vendor] || {};
      const ok = !!(after.ok && after.at && after.at !== before.at);
      const cur = (await liveGet(vendor)) || { on: true, fails: 0 };
      if (!cur.on) return;
      if (ok) { await liveSet(vendor, { ...cur, fails: 0, lastOkAt: Date.now() }); return; }
      const fails = (cur.fails || 0) + 1;
      await liveSet(vendor, { ...cur, fails });
      if (fails >= LIVE_MAX_FAILS) {
        rlog(vendor + "-live:", fails, "dead cycles -- disabling + nudging");
        await disarmLive(vendor, "max-fails");
        await recapMaybeNudge(vendor);
      }
    } finally { _liveBusy = false; }
  }

  // ON-DEMAND recapture — fired by the AO spreadsheet's per-vendor Refresh button (via
  // so_bridge → TRIGGER_RECAPTURE). Same silent background-tab machinery as the live
  // ticks, but explicit, so it works even when live-mode is off. Single-flight: declines
  // while any recapture is already in flight. Resolves after the capture lands (the
  // content script POSTs fresh power straight to the backend), so the page can refetch.
  async function recaptureNow(vendor) {
    vendor = String(vendor || "").toLowerCase();
    if (!RECAP_VENDORS[vendor]) return { ok: false, error: "unsupported-vendor" };
    // Chint can't be captured silently (needs a site click) — don't open a futile
    // background tab; the page falls back to a plain server refetch, and the owner uses
    // the vendor-name button to open the portal for a real refresh.
    if (vendor === "chint") return { ok: false, error: "chint-foreground-only" };
    if (_liveBusy) return { ok: false, error: "busy" };
    // Claim the single-flight slot SYNCHRONOUSLY — before any await — so two sensors
    // firing in the same SW wake (e.g. dashboard-open + network-restored) can't both pass
    // the _liveBusy check and open two tabs (TOCTOU). The finally clears it on every path.
    _liveBusy = true;
    try {
      const { tenantKey } = await recapSettings();
      if (!tenantKey) return { ok: false, error: "not-paired" };
      const st = await recapGetState();
      if (st && st.running && (Date.now() - (st.startedAt || 0)) < TAB_BUDGET_MS) return { ok: false, error: "busy" };
      const before = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {})[vendor] || {};
      await recaptureVendor(vendor, { budgetMs: 150 * 1000 });   // headroom for a full SSO re-login + redirect + sequential recapture on a lapsed session
      const after = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {})[vendor] || {};
      return { ok: true, captured: !!(after.ok && after.at && after.at !== before.at) };
    } catch (e) {
      return { ok: false, error: String(e && e.message || e) };
    } finally { _liveBusy = false; }
  }
  self.__soRecaptureNow = recaptureNow;
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== "TRIGGER_RECAPTURE") return;
    recaptureNow(msg.vendor).then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;   // async sendResponse — held open through the background capture
  });

  // Owner toggles live-mode from Array Operator.
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== "SET_CHINT_LIVE") return;
    (async () => {
      try { if (msg.on) await armChintLive(); else await disarmChintLive("toggle-off"); sendResponse({ ok: true, on: !!msg.on }); }
      catch (e) { sendResponse({ ok: false, error: String(e && e.message || e) }); }
    })();
    return true;
  });

  async function recapSettings() {
    const s = await chrome.storage.local.get([STORAGE_KEYS.TENANT_KEY, STORAGE_KEYS.ENDPOINT]);
    return {
      tenantKey: s[STORAGE_KEYS.TENANT_KEY] || "",
      base: (s[STORAGE_KEYS.ENDPOINT] || PROD_ENDPOINT).replace(/\/v1\/sync$/, ""),
    };
  }

  // POST a captured inverter payload straight to the backend (no open page needed).
  // The endpoint is dual-auth, so the extension's stored tenant key authenticates it.
  async function recapPost(provider, sites) {
    const { tenantKey, base } = await recapSettings();
    if (!tenantKey) { rlog("no tenant key — skipping POST"); return false; }
    if (!Array.isArray(sites) || !sites.length) return false;
    try {
      const r = await fetch(`${base}/v1/array-owners/inverter-capture`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${tenantKey}` },
        body: JSON.stringify({ provider, sites }),
      });
      rlog("POST", provider, "->", r.status, r.ok ? "ok" : "FAIL");
      return r.ok;
    } catch (e) {
      rlog("POST threw", provider, e && e.message || e);
      return false;
    }
  }

  async function recapSetState(st) { try { await chrome.storage.local.set({ [STATE_KEY]: st }); } catch (_) {} }
  async function recapGetState() { const s = await chrome.storage.local.get(STATE_KEY); return s[STATE_KEY] || null; }
  async function recapClearState() { try { await chrome.storage.local.remove(STATE_KEY); } catch (_) {} }

  // ── Recap-tab REAPER — kills orphaned background capture tabs ────────────────
  // Why tabs piled up (Ford: "tons and tons of tabs"): the single-slot so_recap_state
  // only tracks the LATEST recap, and the setTimeout close-watchdog dies when Chrome
  // terminates the idle MV3 worker (~30s, well under the 150s budget) — so a tab whose
  // capture never lands is orphaned and never closed. Fix: track EVERY recap surface in
  // a list and reap any that outlive the budget on a 1-min chrome.alarm (alarms WAKE the
  // worker, so the reap fires even after it was killed) + on browser startup/update.
  const RECAP_TABS_KEY = "so_recap_tabs";   // [{tabId, windowId, openedAt}]
  async function _recapTabsGet() {
    const s = await chrome.storage.local.get(RECAP_TABS_KEY);
    return Array.isArray(s[RECAP_TABS_KEY]) ? s[RECAP_TABS_KEY] : [];
  }
  async function _recapTabsSet(list) {
    try { await chrome.storage.local.set({ [RECAP_TABS_KEY]: list }); } catch (_) {}
  }
  function _closeRecapSurface(e) {
    if (e && typeof e.windowId === "number") {
      try { chrome.windows.remove(e.windowId, () => void chrome.runtime.lastError); } catch (_) {}
    } else if (e && typeof e.tabId === "number") {
      try { chrome.tabs.remove(e.tabId, () => void chrome.runtime.lastError); } catch (_) {}
    }
  }
  async function recapTrackTab(tabId, windowId) {
    if (typeof tabId !== "number") return;
    const list = await _recapTabsGet();
    list.push({ tabId, windowId: (typeof windowId === "number" ? windowId : null), openedAt: Date.now() });
    await _recapTabsSet(list);
  }
  async function recapUntrackTab(tabId) {
    if (typeof tabId !== "number") return;
    const list = await _recapTabsGet();
    const keep = list.filter((e) => e.tabId !== tabId);
    if (keep.length !== list.length) await _recapTabsSet(keep);
  }
  // Close + untrack every recap surface open longer than maxAgeMs (an orphan whose
  // close-watchdog never fired). A fresh, mid-capture tab (< maxAgeMs) is left alone.
  async function reapStaleRecapTabs(maxAgeMs) {
    const now = Date.now();
    const list = await _recapTabsGet();
    if (!list.length) return;
    const fresh = [];
    for (const e of list) {
      if ((now - (e.openedAt || 0)) > maxAgeMs) _closeRecapSurface(e);
      else fresh.push(e);
    }
    if (fresh.length !== list.length) await _recapTabsSet(fresh);
  }
  // Browser startup/update: any TRACKED recap predates this wake, so none is genuinely
  // in-flight — close them all + clear the list.
  async function reapTrackedRecapTabs() {
    const list = await _recapTabsGet();
    list.forEach(_closeRecapSurface);
    await _recapTabsSet([]);
  }
  // One-time cleanup of the EXISTING pile (tabs the pre-reaper builds already orphaned,
  // which aren't in the tracked list): sweep the recap hostnames and close INACTIVE,
  // unpinned, unfocused tabs sitting on them. Conservative — never touches the user's
  // active tab. Run on install/update only, not every startup.
  function reapLegacyVendorTabs() {
    try {
      const pats = Object.values(RECAP_VENDORS).map((u) => {
        try { return "*://" + new URL(u).hostname + "/*"; } catch (_) { return null; }
      }).filter(Boolean);
      if (!pats.length) return;
      chrome.tabs.query({ url: pats, active: false, pinned: false }, (tabs) => {
        if (chrome.runtime.lastError || !Array.isArray(tabs)) return;
        for (const t of tabs) {
          if (t && !t.highlighted && typeof t.id === "number") {
            try { chrome.tabs.remove(t.id, () => void chrome.runtime.lastError); } catch (_) {}
          }
        }
      });
    } catch (_) {}
  }

  async function recapRecordLast(vendor, ok, sites) {
    try {
      if (ok && vendor) { await autoLoginFailsSet(vendor, 0); await keepwarmFailsSet(vendor, 0); }   // a capture landed → clear the auto-login pause + keep-warm give-up counters
      const s = await chrome.storage.local.get(LAST_KEY);
      const m = s[LAST_KEY] || {};
      m[vendor] = { at: new Date().toISOString(), ok, sites: Array.isArray(sites) ? sites.length : 0 };
      await chrome.storage.local.set({ [LAST_KEY]: m });
    } catch (_) {}
  }

  // One gentle reconnect nudge per vendor per day, only when a silent recap failed
  // (almost always an expired portal session the owner must re-auth once).
  async function recapMaybeNudge(vendor) {
    try {
      // Auto-login is the recovery mechanism now, and the Array Operator dashboard's
      // freshness chip is the honest in-app staleness signal — so the intrusive OS
      // "one-tap reconnect" toast is just noise (Ford: "these won't be necessary when we
      // get this working — remove them"). Stay silent for every vault vendor EXCEPT the
      // one genuinely-actionable case: auto-login tried with a saved password and GAVE UP
      // (paused after repeated failures = a wrong/stale password the owner must fix).
      if (typeof SoVault !== "undefined" && _vaultAccepts(vendor)) {
        const fails = await autoLoginFailsGet(vendor);
        if (fails < AUTOLOGIN_MAX_VENDOR_FAILS) {
          rlog("reconnect nudge suppressed for", vendor, "(auto-login era; in-app freshness chip signals staleness; fails=" + fails + ")");
          return;
        }
      }
      const today = new Date().toISOString().slice(0, 10);
      const s = await chrome.storage.local.get(NUDGE_KEY);
      const m = s[NUDGE_KEY] || {};
      if (m[vendor] === today) return;          // already nudged today
      m[vendor] = today;
      await chrome.storage.local.set({ [NUDGE_KEY]: m });
      const label = vendor === "fronius" ? "Fronius Solar.web"
        : vendor === "sma" ? "SMA Sunny Portal"
        : vendor === "chint" ? "Chint"
        : vendor === "gmp" ? "Green Mountain Power"
        : _isUtilityCode(vendor) ? _utilityLabel(vendor) : vendor;
      // Reaches here ONLY when auto-login gave up (saved password failing) — so the copy
      // is the honest "your saved password isn't working", not a generic refresh prompt.
      chrome.notifications.create(`recap-${vendor}-${today}`, {
        type: "basic",
        iconUrl: "icons/icon128.png",
        title: "EnergyAgent: reconnect needed",
        message: `Your saved ${label} password isn't signing in. Click to open ${label} and sign in once to refresh — then re-save it in the extension.`,
        requireInteraction: true,   // stay until he acts; this is his one-click recovery
      });
    } catch (_) {}
  }

  // ONE-CLICK RECOVERY: clicking the reconnect notification opens that vendor's
  // portal as an ACTIVE tab with capture already armed. He signs in once; the
  // content script rides the fresh session, captures, and we're silent again for
  // weeks. No stored passwords — just a frictionless re-auth when a session lapses.
  chrome.notifications.onClicked.addListener((notifId) => {
    const m = String(notifId || "").match(/^recap-(fronius|sma|chint)-/);
    if (!m) return;
    const vendor = m[1];
    const url = RECAP_VENDORS[vendor];
    if (!url) return;
    (async () => {
      try { await chrome.storage.local.set({ so_capture_intent: { vendor, ts: Date.now() } }); } catch (_) {}
      // Mark a recap in flight (no tabId — it's a foreground tab he controls) so the
      // existing *_CAPTURED hook POSTs the result with the tenant key even if no AO
      // dashboard page is open. recapFinish won't try to close his tab (tabId absent).
      await recapSetState({ running: true, vendor, tabId: null, startedAt: Date.now() });
      chrome.tabs.create({ url, active: true }, () => void chrome.runtime.lastError);  // foreground so he can log in
      try { chrome.notifications.clear(notifId, () => void chrome.runtime.lastError); } catch (_) {}
    })();
  });

  // Close a background recap tab and clear the in-flight state.
  async function recapFinish(vendor, ok, sites) {
    const st = await recapGetState();
    await recapRecordLast(vendor, ok, sites);
    if (!ok) await recapMaybeNudge(vendor);
    // Close the recap surface the instant we're done. For a separate background
    // window (chint live-mode) remove the WHOLE window so it can't linger as an
    // empty/minimized frame; otherwise just the background tab. (Ford: it must
    // delete itself when done -- not pop up and stick around.)
    if (st && typeof st.windowId === "number") {
      try { chrome.windows.remove(st.windowId, () => void chrome.runtime.lastError); } catch (_) {}
    } else if (st && typeof st.tabId === "number") {
      try { chrome.tabs.remove(st.tabId, () => void chrome.runtime.lastError); } catch (_) {}
    }
    if (st && typeof st.tabId === "number") await recapUntrackTab(st.tabId);
    await recapClearState();
  }

  // Close any recapture tab/window left over from a PRIOR cycle. The MV3 service worker
  // can be terminated before the close-watchdog setTimeout fires, orphaning the surface;
  // without this they accumulate (Ford's "bunch of chint tabs"). Safe because callers
  // only reach recaptureVendor after the single-flight check, so any existing state is
  // stale. Called before opening a new surface + on install/update.
  async function reapOrphanRecapture() {
    const st = await recapGetState();
    if (!st) return;
    if (typeof st.windowId === "number") { try { chrome.windows.remove(st.windowId, () => void chrome.runtime.lastError); } catch (_) {} }
    else if (typeof st.tabId === "number") { try { chrome.tabs.remove(st.tabId, () => void chrome.runtime.lastError); } catch (_) {} }
    await recapClearState();
  }
  self.__soReapOrphanRecapture = reapOrphanRecapture;

  // Open ONE vendor's portal in a background tab, arm the capture intent, and let
  // the existing content script do its thing. A watchdog closes the tab if the
  // capture never lands (expired session) and fires the gentle nudge.
  // ── COOKIE HYGIENE (v1.9.76) ────────────────────────────────────────────────
  // Repeated auto-logins make WSO2 (Fronius) / Keycloak (SMA) pile up session/auth
  // cookies; left unchecked the request-header cookie blob eventually outgrows the
  // portal's HTTP/2 header limit and the SITE STOPS LOADING ENTIRELY
  // (ERR_HTTP2_PROTOCOL_ERROR — Ford hit exactly this on solarweb.com after a marathon
  // of test logins). Before each recapture we measure the vendor's cookie size and, ONLY
  // if it's bloated past a safe ceiling (a normal session is ~1-2KB), clear it so the
  // recapture's auto-login re-establishes a clean, minimal session. Targeted: a healthy
  // session is NEVER touched (no forced re-login) — we only intervene when the blob is
  // heading for the breaking point, which is strictly better than the portal going
  // unloadable. Our customers log in far more often than a human (keep-warm re-auths), so
  // this is what keeps the flagship's "never touch your portal again" from breaking later.
  const VENDOR_COOKIE_DOMAINS = {
    fronius: ["solarweb.com", "fronius.com"],
    sma: ["sunnyportal.com", "sma.energy"],
  };
  const COOKIE_BLOAT_BYTES = 8192;   // well above a normal session (~1-2KB), well below the ~16KB+ that breaks HTTP/2
  const COOKIE_PRUNE_COOLDOWN_MS = 20 * 60 * 1000;
  const COOKIE_PRUNE_AT_KEY = "so_cookie_pruned_at";   // { fronius: ts, sma: ts } — anti-loop
  async function cookiePruneEnabled() {
    try { const s = await chrome.storage.local.get("so_cookieprune_off"); return s.so_cookieprune_off !== true; } catch (_) { return true; }
  }
  async function vendorCookieBytes(vendor) {
    const domains = VENDOR_COOKIE_DOMAINS[vendor];
    if (!domains) return { bytes: 0, cookies: [] };
    let bytes = 0; const cookies = [];
    for (const domain of domains) {
      try {
        for (const c of await chrome.cookies.getAll({ domain })) {
          bytes += (c.name || "").length + (c.value || "").length + 4;   // ~"name=value; "
          cookies.push(c);
        }
      } catch (_) {}
    }
    return { bytes, cookies };
  }
  self.__soVendorCookieBytes = async (v) => (await vendorCookieBytes(v)).bytes;
  // Clear the vendor's cookies IFF they've bloated past the ceiling. Returns how many it
  // removed (0 = healthy, left alone). Clearing a bloated, about-to-break session is
  // strictly better than letting the portal become unloadable; auto-login (or a one-time
  // manual sign-in) re-establishes a clean one.
  async function pruneVendorCookiesIfBloated(vendor) {
    try {
      if (!VENDOR_COOKIE_DOMAINS[vendor] || !(await cookiePruneEnabled())) return 0;
      const { bytes, cookies } = await vendorCookieBytes(vendor);
      if (bytes < COOKIE_BLOAT_BYTES) return 0;   // healthy — never touch a working session
      // Anti-loop: never prune the same vendor more than once per cooldown, so even if a
      // single fresh login were itself large we can't force a re-login on every recapture.
      const at = (await chrome.storage.local.get(COOKIE_PRUNE_AT_KEY))[COOKIE_PRUNE_AT_KEY] || {};
      if (at[vendor] && (Date.now() - at[vendor]) < COOKIE_PRUNE_COOLDOWN_MS) {
        rlog("cookie-hygiene:", vendor, "blob ~" + bytes + "B but pruned recently — cooldown, skipping");
        return 0;
      }
      let n = 0;
      for (const c of cookies) {
        const u = (c.secure ? "https://" : "http://") + String(c.domain || "").replace(/^\./, "") + (c.path || "/");
        try { await chrome.cookies.remove({ url: u, name: c.name, storeId: c.storeId }); n++; } catch (_) {}
      }
      at[vendor] = Date.now();
      try { await chrome.storage.local.set({ [COOKIE_PRUNE_AT_KEY]: at }); } catch (_) {}
      rlog("cookie-hygiene:", vendor, "cookie blob ~" + bytes + "B > " + COOKIE_BLOAT_BYTES + "B — cleared", n, "cookies to prevent ERR_HTTP2_PROTOCOL_ERROR (auto-login re-establishes a clean session)");
      return n;
    } catch (e) { rlog("cookie-hygiene error", vendor, e && e.message || e); return 0; }
  }
  self.__soPruneCookies = pruneVendorCookiesIfBloated;

  async function recaptureVendor(vendor, opts) {
    const url = _recapUrlFor(vendor);            // inverter vendor OR utility portal URL
    if (!url) return;
    await pruneVendorCookiesIfBloated(vendor);   // clear a bloated cookie blob BEFORE it breaks the portal load (no-op for utilities)
    await reapOrphanRecapture();   // never stack a new surface on a leftover one
    const newWindow = !!(opts && opts.newWindow);
    const budgetMs = (opts && typeof opts.budgetMs === "number") ? opts.budgetMs : TAB_BUDGET_MS;
    try { await chrome.storage.local.set({ so_capture_intent: { vendor, ts: Date.now() } }); } catch (_) {}
    await new Promise((resolve) => {
      // Arm the watchdog + in-flight state once we have the tab id, whichever
      // surface opened it. recapFinish closes the tab (and, for a one-tab window,
      // the whole window) when the capture lands or the watchdog times out.
      const armed = async (tabId, windowId) => {
        if (tabId == null) { await recapMaybeNudge(vendor); resolve(); return; }
        await recapSetState({ running: true, vendor, tabId, windowId: (typeof windowId === "number" ? windowId : null), startedAt: Date.now() });
        await recapTrackTab(tabId, windowId);   // so the alarm reaper can kill it even if this worker dies before the watchdog
        setTimeout(async () => {
          const st = await recapGetState();
          if (st && st.running && st.vendor === vendor && st.tabId === tabId) {
            rlog("watchdog timeout for", vendor, "(likely expired session)");
            await recapFinish(vendor, false, []);
          }
          resolve();
        }, budgetMs);
      };
      if (newWindow) {
        // CHINT live-mode: refresh in a SEPARATE minimized, unfocused popup window so
        // it never steals focus or adds a tab to the owner's window -- as close to
        // invisible as MV3 allows. recapFinish removes the WHOLE window the instant
        // capture lands (or the short watchdog fires), so it self-deletes.
        chrome.windows.create({ url, focused: false, state: "minimized", type: "popup" }, (win) => {
          const tab = win && win.tabs && win.tabs[0];
          if (chrome.runtime.lastError || !win || !tab) { armed(null); return; }
          armed(tab.id, win.id);
        });
      } else {
        chrome.tabs.create({ url, active: false }, (tab) => {   // background tab in the current window
          if (chrome.runtime.lastError || !tab) { armed(null); return; }
          armed(tab.id);
        });
      }
    });
  }

  // ----- AUTO-LOGIN (client-side creds, opt-out) -----------------------------
  // The injected function that drives the portal's OWN login form. Returns
  // "submitted" | "no-form" | "already-in". Defined here (not a file) so
  // executeScript({func,args}) can pass the decrypted creds straight into the page
  // without writing them to disk. The creds live only in the worker's memory for
  // this call and are NEVER sent to our backend — we just type into the real form.
  // Returns a Promise (executeScript awaits it) of:
  //   "submitted"       — typed username+password and triggered submit
  //   "filled-username" — identifier-first step: typed username + clicked continue
  //                       (the password step arrives via a later navigation/retry)
  //   "no-form"         — no usable field appeared (page still loading / wrong page)
  //   "already-in"      — a logged-in dashboard (no login form) — nothing to do
  // It POLLS for the form (a server-rendered SSO page can lag the "complete" event)
  // and, for an identifier-first flow, waits in-place for the password step so a
  // single call can finish a two-step WSO2/Keycloak login when it AJAXes the second
  // step in. The creds live only in this call's arguments — never written to disk,
  // never sent to our backend; we just type into the portal's OWN form.
  async function soFillLoginForm(username, password, vendor) {
    if (!username || !password) return "no-form";
    // Grounded per-vendor selectors (verified 2026-06-16 against the live login
    // DOMs): SMA Keycloak login.sma.energy → #username/#password; Fronius WSO2
    // login(.online).fronius.com → #usernameUserInput/#password. PRIMARY path; the
    // generic matcher is the fallback if a portal reworks its form.
    const HINTS = {
      sma: { user: "#username", pass: "#password", btn: "#kc-login, button[type=\"submit\"]" },
      fronius: { user: "#usernameUserInput", pass: "#password", btn: "#login-button, [data-testid=\"login-page-continue-login-button\"], button[type=\"submit\"]" },
      // v1.9.97 — UTILITY login selectors. ⚠️ BEST-EFFORT, pending real-browser
      // verification (the operator's first sign-out test; the SW console logs
      // `auto-login OUTCOME for <code> => …`). GMP is a standard email+password
      // form; SmartHub/NISC portals share one platform — classic ASP.NET ids
      // (#LoginUsernameTextBox / #LoginPasswordTextBox) on older skins, and
      // name=username / name=password on newer ones. ALL of these are plain
      // username+password+submit forms, so the GENERIC matcher below almost
      // certainly already handles them; these hints just give the happy path a
      // head start. If MFA/CAPTCHA is present the fill can't bypass it and the
      // existing fail-pause guard applies (we never hammer).
      gmp: { user: "input[type=\"email\"], #username, input[name=\"username\" i], input[name=\"email\" i]", pass: "#password, input[name=\"password\" i]", btn: "button[type=\"submit\"], button[id*=\"login\" i], button[id*=\"signin\" i]" },
      smarthub: { user: "#LoginUsernameTextBox, input[name=\"username\" i], input[name=\"userId\" i], input[type=\"email\"]", pass: "#LoginPasswordTextBox, input[name=\"password\" i], input[type=\"password\"]", btn: "#LoginSubmitButton, button[type=\"submit\"], input[type=\"submit\"], button[id*=\"login\" i]" },
    };
    // This function is INJECTED into the page (MAIN world) — it can't call the
    // service-worker helpers, so derive the hint key from the code shape alone:
    // a known inverter/gmp key uses its own hint; any OTHER code passed here is a
    // SmartHub co-op (vec/wec/sh_*) and shares the one SmartHub login form.
    const hintKey = HINTS[vendor] ? vendor
      : (vendor === "fronius" || vendor === "sma" || vendor === "chint") ? vendor
      : "smarthub";
    const hint = HINTS[hintKey] || null;
    const vis = (el) => el && el.offsetParent !== null && !el.disabled;
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const findUser = () => {
      if (hint) { const u = document.querySelector(hint.user); if (vis(u)) return u; }
      return Array.from(document.querySelectorAll(
        'input[type="text"], input[type="email"], input[type="tel"], input[name*="user" i], input[name*="email" i], input[id*="user" i], input[id*="email" i]'
      )).filter((el) => vis(el) && el.type !== "password")[0] || null;
    };
    const findPass = () => {
      if (hint) { const p = document.querySelector(hint.pass); if (vis(p)) return p; }
      return Array.from(document.querySelectorAll('input[type="password"]')).filter(vis)[0] || null;
    };
    const findBtn = (scope) => {
      const root = scope || document;
      if (hint && hint.btn) { const b = root.querySelector(hint.btn); if (vis(b)) return b; }
      return root.querySelector(
        'button[type="submit"], input[type="submit"], button[name*="login" i], button[id*="login" i], button[id*="signin" i], button[id*="next" i], button[id*="continue" i]'
      );
    };
    const setVal = (el, val) => {
      const proto = el.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, val);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const submit = (btn, ref) => setTimeout(() => {
      try {
        if (btn && btn.offsetParent !== null) { btn.click(); return; }
        if (ref && ref.form && ref.form.requestSubmit) { ref.form.requestSubmit(); return; }
        if (ref) ref.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
      } catch (_) {}
    }, 300);

    // Poll up to ~6s for a username/password field to appear.
    let pw = null, user = null;
    for (let i = 0; i < 20; i++) {
      pw = findPass(); user = findUser();
      if (pw || user) break;
      await sleep(300);
    }
    if (!pw && !user) return "no-form";

    if (pw && user) {                       // combined form (the grounded happy path)
      try { user.focus(); setVal(user, username); pw.focus(); setVal(pw, password); } catch (_) { return "no-form"; }
      submit(findBtn(pw.form), pw);
      return "submitted";
    }
    if (pw && !user) {                       // password-only second step
      try { pw.focus(); setVal(pw, password); } catch (_) { return "no-form"; }
      submit(findBtn(pw.form), pw);
      return "submitted";
    }
    // Identifier-first step (WSO2/Keycloak): type username, continue, then wait for
    // the password step to render (it may AJAX in OR navigate — if it navigates this
    // call's context is torn down and the onUpdated trigger re-fires for step two).
    try { user.focus(); setVal(user, username); } catch (_) { return "no-form"; }
    submit(findBtn(user.form), user);
    for (let i = 0; i < 20; i++) {
      await sleep(300);
      const p2 = findPass();
      if (p2) {
        try { p2.focus(); setVal(p2, password); } catch (_) { return "filled-username"; }
        submit(findBtn(p2.form), p2);
        return "submitted";
      }
    }
    return "filled-username";
  }

  // Is `tabId` a background tab WE opened for capture (single-surface recapture OR a
  // concurrent "Sync all" tab)? Returns its vendor, else null. This is the security
  // gate: auto-login ONLY ever fills a tab we control — never the owner's own tabs.
  async function _captureTabVendor(tabId) {
    if (typeof tabId !== "number") return null;
    const op = _openPortalTabs.get(tabId); if (op) return op;                  // dashboard "sync" chip background tab
    try { const sy = _syncTabs.get(tabId); if (sy && sy.vendor) return sy.vendor; } catch (_) {}   // "Sync all" tab
    const st = await recapGetState();
    if (st && st.running && st.tabId === tabId && st.vendor) return st.vendor;  // single-surface recapture tab
    return null;
  }

  // Vendor SSO login origins a dead session redirects to. These have NO content
  // script (different origin from the dashboard), so the chrome.tabs.onUpdated hook
  // below is the ONLY thing that can drive their login form.
  // Match ANY Fronius/SMA identity-provider host: the dashboards live on solarweb.com
  // and sunnyportal.com, so once one of OUR fronius/sma capture tabs lands on a
  // fronius.com / sma.energy host it IS the auth flow (login.fronius.com / auth.fronius.com
  // [WSO2], login.sma.energy [Keycloak] — verified login.online.fronius.com does NOT resolve,
  // so don't key on it). Anchored with (^|\.) so a lookalike like "notfronius.com" or
  // "fronius.com.attacker.net" can't match; _captureTabVendor is the second gate.
  // v1.9.97: utilities added. GMP logs in SAME-ORIGIN (greenmountainpower.com),
  // so its dead-session form is reached BOTH by this nav trigger AND by the
  // content script's LOGIN_STATE_DETECTED{provider:"gmp"} (the Chint-style
  // secondary trigger). SmartHub co-ops ALSO log in same-origin
  // (*.smarthub.coop) — a single host pattern can serve any of ~470 co-ops, so
  // a static vendor string won't do: we resolve the host to its co-op CODE
  // (vec/wec/sh_*) via the registry (smartHubCodeForHost), then drive auto-login
  // with that co-op's saved credential. Anchored with (^|\.) so a lookalike
  // can't match; _captureTabVendor is the second gate (we only ever fill OUR
  // own capture tabs, never the owner's tabs).
  const _LOGIN_HOSTS = [
    { re: /(^|\.)fronius\.com$/i, vendor: "fronius" },
    { re: /(^|\.)sma\.energy$/i, vendor: "sma" },
    { re: /(^|\.)greenmountainpower\.com$/i, vendor: "gmp" },
    // SmartHub: vendor is resolved per-host from the registry (see resolve()).
    { re: /(^|\.)smarthub\.coop$/i, vendor: "smarthub", resolve: (h) => {
        try { if (typeof self.smartHubCodeForHost === "function") return self.smartHubCodeForHost(h); } catch (_) {}
        // Fallback if the registry didn't load: the two grounded co-ops, else a
        // deterministic discovered code so a brand-new co-op still resolves.
        if (h === "vermontelectric.smarthub.coop") return "vec";
        if (h === "washingtonelectric.smarthub.coop") return "wec";
        const m = /^([^.]+)\.smarthub\.coop$/i.exec(h);
        return m ? "sh_" + m[1].replace(/[^a-z0-9]+/gi, "_").toLowerCase().slice(0, 37) : null;
      } },
  ];
  function _loginVendorForUrl(url) {
    let h; try { h = new URL(url).hostname; } catch (_) { return null; }
    for (const e of _LOGIN_HOSTS) {
      if (!e.re.test(h)) continue;
      return typeof e.resolve === "function" ? e.resolve(h) : e.vendor;
    }
    return null;
  }
  function _safeHost(url) { try { return new URL(url).hostname; } catch (_) { return String(url || "").slice(0, 60); } }
  // v1.9.97: does the vault accept this code (inverter vendor OR utility)? Uses the
  // vault's own accepts() when present (newer builds), else falls back to the
  // VENDORS list + a local utility check, so an older vault never breaks this path.
  function _isUtilityCode(code) {
    try { if (typeof SoVault !== "undefined" && typeof SoVault.isUtilityCode === "function") return SoVault.isUtilityCode(code); } catch (_) {}
    const c = String(code || "").toLowerCase();
    if (c === "gmp" || c.startsWith("sh_") || c === "vec" || c === "wec") return true;
    try { if (typeof self.SMARTHUB_REGISTRY === "object") return Object.values(self.SMARTHUB_REGISTRY).some((e) => e && e.provider === c); } catch (_) {}
    return false;
  }
  function _vaultAccepts(code) {
    if (typeof SoVault === "undefined") return false;
    try { if (typeof SoVault.accepts === "function") return SoVault.accepts(code); } catch (_) {}
    try { if (SoVault.VENDORS && SoVault.VENDORS.includes(code)) return true; } catch (_) {}
    return _isUtilityCode(code);
  }
  // Friendly display name for a utility code (for nudges). Reads the co-op's name
  // from the registry when available; falls back to the upper-cased code.
  function _utilityLabel(code) {
    const c = String(code || "").toLowerCase();
    if (c === "gmp") return "Green Mountain Power";
    try {
      const reg = self.SMARTHUB_REGISTRY;
      if (reg) { for (const k of Object.keys(reg)) { if (reg[k] && reg[k].provider === c) return reg[k].name || c.toUpperCase(); } }
    } catch (_) {}
    return c.toUpperCase();
  }

  // Fill + submit a vendor's login form on one of OUR capture tabs using the vault
  // creds. Guards: vault holds creds + auto-login enabled (opt-out); never resubmit
  // the SAME tab (lockout-loop guard); cap form-not-ready retries per tab; and PAUSE
  // a vendor after AUTOLOGIN_MAX_VENDOR_FAILS submits that didn't yield a capture
  // (wrong password / changed form) so a 6-min live loop can't keep hammering a bad
  // password across fresh tabs and lock the account. Cleared on any success or a
  // creds re-save. On a real submit the portal redirects back to the dashboard, the
  // content script re-polls the now-valid session, and the normal capture path runs.
  async function tryAutoLoginOnTab(vendor, tabId) {
    try {
      if (typeof tabId !== "number") return;
      // v1.9.97: accept inverter vendors AND utility codes (gmp / SmartHub co-op).
      if (typeof SoVault === "undefined" || !_vaultAccepts(vendor)) return;
      if (_autoLoginSubmittedTab.has(tabId)) {
        // We already submitted on this tab and it's BACK on a login page — the saved
        // password didn't take. Count the fail NOW (only on this confirmed re-presentation),
        // not optimistically at submit time, so a slow-but-successful sign-in (whose capture
        // lands a beat later) never pauses good creds. A real success redirects AWAY from the
        // login host, so this handler never fires for it; the capture then resets fails to 0.
        await autoLoginFailsSet(vendor, (await autoLoginFailsGet(vendor)) + 1);
        rlog("auto-login: login re-presented after submit for", vendor, "— counted a real fail");
        return;
      }
      const fails = await autoLoginFailsGet(vendor);
      if (fails >= AUTOLOGIN_MAX_VENDOR_FAILS) {
        rlog("auto-login: PAUSED for", vendor, "after", fails, "failed attempts — re-save the password (or sign in once) to retry");
        broadcastToSoTabs({ type: "SO_LOGIN_STATE", provider: vendor, state: "login_required", reason: "auto-login-paused" });
        return;
      }
      const attempts = _autoLoginAttemptsTab.get(tabId) || 0;
      if (attempts >= AUTOLOGIN_MAX_TAB_ATTEMPTS) { rlog("auto-login: max attempts on tab", tabId, "for", vendor); return; }
      if (!(await SoVault.isEnabled(vendor))) { rlog("auto-login: opted out for", vendor); return; }
      const creds = await SoVault.get(vendor);
      if (!creds) {
        rlog("auto-login: no stored creds for", vendor, "(one-click recovery will nudge)");
        // No saved password → we can't sign in silently. Tell the AO page so it surfaces a
        // one-click "Sign in to add <vendor>" instead of a tab that sits invisibly on the SSO
        // login page (the SSO origin has no content script to emit this itself).
        broadcastToSoTabs({ type: "SO_LOGIN_STATE", provider: vendor, state: "login_required", reason: "no-creds" });
        // Don't leave a Sync-all background tab grinding on the login page — close it so the
        // page's "Sign in to add" is the clean next step. Only reap bare _syncTabs tabs; a
        // recap surface (Chint's popup) self-manages via its own watchdog.
        if (_syncTabs.has(tabId)) { try { _syncTabs.delete(tabId); chrome.tabs.remove(tabId, () => void chrome.runtime.lastError); } catch (_) {} }
        return;
      }
      _autoLoginAttemptsTab.set(tabId, attempts + 1);
      rlog("auto-login: ATTEMPT", attempts + 1, "— filling", vendor, "login form on tab", tabId);
      const res = await chrome.scripting.executeScript({
        target: { tabId },
        func: soFillLoginForm,
        args: [creds.username, creds.password, vendor],
        world: "MAIN",   // page context so the portal framework sees the input events (creds are
                         // visible only to the genuine vendor login page — same as typing manually)
      });
      const outcome = res && res[0] && res[0].result;
      rlog("auto-login OUTCOME for", vendor, "=>", outcome);
      if (outcome === "submitted") {
        _autoLoginSubmittedTab.add(tabId);
        // Do NOT count a fail here. A fail is only real once the login page RE-PRESENTS on
        // this tab (handled at the top of this function); a success redirects to the dashboard
        // and lands a capture (which resets the counter). This stops a slow-but-good sign-in
        // from racking up "fails" across ticks and pausing valid credentials (the SMA bug).
      }
      // "filled-username" / "no-form" / "already-in": leave the tab un-submitted so a
      // later navigation (e.g. the password step) can retry, up to the attempt cap.
    } catch (e) {
      rlog("auto-login error", vendor, "tab", tabId, e && e.message || e);
    }
  }
  self.__soTryAutoLoginOnTab = tryAutoLoginOnTab;
  self.__soAutoLoginResetFails = (v) => { try { if (v) autoLoginFailsSet(v, 0); } catch (_) {} };
  self.__soKeepwarmResetFails = (v) => { try { if (v) keepwarmFailsSet(v, 0); } catch (_) {} };
  // Register a BACKGROUND SO_OPEN_PORTAL capture tab (the dashboard "sync" chip) so the
  // nav-driven auto-login recognizes it as ours. Called from the OPEN_UTILITY_PORTAL
  // handler. Only fronius/sma here because their login is on a SEPARATE SSO origin that
  // needs the chrome.tabs.onUpdated (nav) trigger. Chint logs in SAME-ORIGIN
  // (monitor.chintpowersystems.com), so its auto-login fires via the SECONDARY trigger
  // instead — chint_content.js broadcasts LOGIN_STATE_DETECTED{login_required} → the
  // handler → recapTryAutoLogin (recognizes the chint recap tab via so_recap_state). So
  // Chint IS auto-login-capable now (v1.9.87); it just doesn't need this nav registration.
  // SolarEdge also logs in same-origin.
  self.__soRegisterAutoLoginTab = (tabId, vendor) => {
    try { if (typeof tabId === "number" && (vendor === "fronius" || vendor === "sma")) _openPortalTabs.set(tabId, vendor); } catch (_) {}
  };

  // PRIMARY TRIGGER (v1.9.71): when one of OUR capture tabs navigates to a vendor's
  // SSO login page, fill the form. This is what makes auto-login actually fire on a
  // DEAD session — the dashboard's content script can't, because the login lives on a
  // different origin it isn't injected into. Fires for BOTH the single-surface
  // recapture AND the concurrent "Sync all" tabs (gap #2). Every other tab is ignored.
  chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (!changeInfo || changeInfo.status !== "complete") return;   // wait until the form is in the DOM
    const url = (tab && tab.url) || changeInfo.url;
    if (!url || !_loginVendorForUrl(url)) return;                  // not a vendor SSO login page → ignore
    (async () => {
      const ourVendor = await _captureTabVendor(tabId);
      if (!ourVendor) return;                                      // not a tab we opened for capture → never touch it
      rlog("auto-login: our", ourVendor, "capture tab", tabId, "landed on SSO login page", _safeHost(url), "— attempting auto-login");
      await tryAutoLoginOnTab(ourVendor, tabId);
    })().catch((e) => rlog("auto-login(nav) error", e && e.message || e));
  });
  // Forget per-tab auto-login state when the tab closes (keeps the sets bounded).
  chrome.tabs.onRemoved.addListener((tabId) => {
    _autoLoginSubmittedTab.delete(tabId);
    _autoLoginAttemptsTab.delete(tabId);
    _openPortalTabs.delete(tabId);
  });

  // SECONDARY TRIGGER: a content script reported the session is gone while still on
  // the dashboard origin (some portals show login same-origin). Route it through the
  // same engine, gated to OUR capture tabs (covers single-surface AND sync-all).
  async function recapTryAutoLogin(vendor, tabId, state) {
    try {
      if (state !== "login_required") return;
      if (typeof tabId !== "number") return;
      const ourVendor = await _captureTabVendor(tabId);
      if (!ourVendor) { rlog("auto-login: login_required on tab", tabId, "— not one of our capture tabs, ignoring"); return; }
      await tryAutoLoginOnTab(ourVendor, tabId);
    } catch (e) { rlog("auto-login(msg) error", e && e.message || e); }
  }
  // Expose for the LOGIN_STATE_DETECTED handler (outside this IIFE).
  self.__soRecapTryAutoLogin = recapTryAutoLogin;

  // ==========================================================================
  // KEEP-WARM + EVENT SENSORS (v1.9.74) — "always trying to be connected"
  // --------------------------------------------------------------------------
  // Extra reconnect layers stacked ON TOP of live-mode/auto-login, ALL funneled
  // through the SAME governed engine (recaptureNow's single-flight), so adding
  // them can never produce a pile of tabs. Three sensors:
  //   • keep-warm ping  — a tab-less authenticated touch (Fronius, cookie-based)
  //     that exercises the session so it never lapses, and revives it the instant
  //     it does. SMA's session check needs a page-held Bearer token, so it can't be
  //     pinged tab-lessly — SMA stays warm via its 6-min live capture + the two
  //     event sensors below.
  //   • network-restored — when connectivity returns after a gap, refresh used vendors.
  //   • dashboard-open  — when the owner opens/focuses the Array Operator dashboard,
  //     refresh so they see live data the moment they look.
  // ==========================================================================
  const KEEPWARM_ALARM = "keep-warm";
  const KEEPWARM_PERIOD_MIN = 11;                 // between the 6-min live ticks and the hourly sweep
  const KEEPWARM_OFF_KEY = "so_keepwarm_off";     // global kill switch: set true to disable all of this
  const WAS_OFFLINE_KEY = "so_was_offline";
  const _DASH_DEBOUNCE_MS = 90 * 1000;
  let _lastDashKickAt = 0;
  // Cheap, COOKIE-authenticated session-check endpoint (the same one the content script
  // uses for isSignedIn). A tab-less extension-SW fetch with credentials rides the owner's
  // portal cookie via host_permissions. Fronius only (SMA's check is Bearer-token gated).
  const KEEPWARM_PING = { fronius: "https://www.solarweb.com/Messages/GetUnreadMessageCountForUser" };

  async function keepWarmEnabled() {
    try { const s = await chrome.storage.local.get(KEEPWARM_OFF_KEY); return s[KEEPWARM_OFF_KEY] !== true; } catch (_) { return true; }
  }
  function _isAppUrl(url) {
    try { return /(^|\.)(arrayoperator\.com|nepooloperator\.com|solaroperator\.org)$/i.test(new URL(url).hostname); } catch (_) { return false; }
  }
  // Vendors the owner actually USES (a prior successful capture OR saved auto-login creds),
  // not opted out — the set every keep-warm/refresh sensor acts on.
  async function usedInverterVendors() {
    const known = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY]) || {};
    const out = [];
    for (const v of ["fronius", "sma"]) {
      let used = !!(known[v] && known[v].ok);
      if (!used && typeof SoVault !== "undefined") { try { used = await SoVault.has(v); } catch (_) {} }
      if (!used) continue;
      if (typeof SoVault !== "undefined") { try { if (!(await SoVault.isEnabled(v))) continue; } catch (_) {} }
      out.push(v);
    }
    return out;
  }
  // Keep-warm session PROBE (Fronius, cookie-based): cheaply reports whether the portal
  // session is alive WITHOUT opening a tab — "warm" (2xx), "lapsed" (login redirect / 401 /
  // 403), or "blocked" (network/CORS — ambiguous). Used ONLY to skip a futile tab for a
  // vendor we have no saved creds for; it does NOT keep the data fresh (a 200 cookie ≠ fresh
  // readings, and an AJAX ping does NOT reset a portal's client-side JS idle timer).
  async function keepWarmPing(vendor) {
    const url = KEEPWARM_PING[vendor];
    if (!url) return "no-ping";
    try {
      const r = await fetch(url + (url.includes("?") ? "&" : "?") + "_=" + Date.now(),
        { credentials: "include", redirect: "manual", cache: "no-store" });
      if (r.status >= 200 && r.status < 300) return "warm";
      // 0 = opaqueredirect (3xx → login under redirect:"manual"); a visible 3xx, 401 or 403 also = logged out.
      if (r.status === 0 || (r.status >= 300 && r.status < 400) || r.status === 401 || r.status === 403) return "lapsed";
      return "warm";   // any other non-error status → treat the session as present
    } catch (e) {
      rlog("keep-warm:", vendor, "probe blocked/network —", (e && e.message) || e);
      return "blocked";
    }
  }
  // One keep-warm tick. For every USED vendor whose 6-min live-mode ISN'T already keeping it
  // fresh, REFRESH THE DATA with a real recapture when our last successful capture has gone
  // stale (> KEEPWARM_REFRESH_MIN). This is the v1.9.75 fix for "cookie warm but data frozen":
  // the v1.9.74 tab-less ping confirmed the session yet never advanced last_seen_at/power, and
  // an AJAX ping does NOT reset a portal's CLIENT-SIDE JS idle timer (Solar.web has one) — so
  // Fronius drifted hours stale behind a "warm" cookie. A real recapture opens the portal in a
  // background tab: that full page-load (a) refreshes the readings, (b) resets the JS idle
  // timer, and (c) auto-logs-in if the session lapsed. Governed by the single-flight lock, so
  // it can't pile up tabs. The cheap probe is kept only to skip a FUTILE tab when a vendor we
  // have no creds for is logged out (a tab would just land on the login wall — nudge instead).
  const KEEPWARM_REFRESH_MIN = 9;   // refresh a live-mode-off vendor at most ~once per keep-warm cycle
  async function runKeepWarmTick() {
    if (!(await keepWarmEnabled())) return;
    if (_liveBusy) { rlog("keep-warm: engine busy — skip"); return; }
    const { tenantKey } = await recapSettings();
    if (!tenantKey) return;
    const last = (await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {};
    for (const v of await usedInverterVendors()) {
      if ((await autoLoginFailsGet(v)) >= AUTOLOGIN_MAX_VENDOR_FAILS) continue;   // auto-login paused: failing creds
      if ((await keepwarmFailsGet(v)) >= KEEPWARM_MAX_FAILS) continue;            // gave up: dead session we can't refresh (a success or creds-save re-enables)
      if (!!((await liveGet(v)) || {}).on) continue;                              // live-mode already refreshing
      const rec = last[v];
      const lastAt = (rec && rec.ok && rec.at) ? Date.parse(rec.at) : 0;
      const staleMin = lastAt ? (Date.now() - lastAt) / 60000 : Infinity;
      if (staleMin < KEEPWARM_REFRESH_MIN) continue;                              // data still fresh
      // Skip a futile tab: if we have NO creds to auto-login this vendor and a cheap probe
      // says the session is dead, nudge instead (a tab would only hit the login wall) and
      // count it toward give-up so a perpetually-dead no-creds vendor stops churning tabs.
      let hasCreds = false;
      try { hasCreds = (typeof SoVault !== "undefined") && await SoVault.has(v); } catch (_) {}
      if (KEEPWARM_PING[v] && !hasCreds && (await keepWarmPing(v)) === "lapsed") {
        rlog("keep-warm:", v, "session lapsed + no saved creds → nudge (skip futile tab)");
        await keepwarmFailsSet(v, (await keepwarmFailsGet(v)) + 1);
        await recapMaybeNudge(v);
        continue;
      }
      rlog("keep-warm:", v, "data", (staleMin === Infinity ? "never synced" : Math.round(staleMin) + "m stale"), "→ recapturing (refresh + reset idle timer)");
      let res = null;
      try { res = await recaptureNow(v); } catch (_) {}
      // captured → recapRecordLast(ok) already reset keepwarmFails; ran-but-captured-nothing
      // (a dead session we couldn't revive) climbs toward give-up; ok:false (busy) is transient.
      if (res && res.ok && res.captured === false) await keepwarmFailsSet(v, (await keepwarmFailsGet(v)) + 1);
    }
  }
  self.__soKeepWarm = runKeepWarmTick;
  // Refresh every used vendor NOW through the governed engine (declines per-vendor if busy).
  async function kickRefreshUsedVendors(reason) {
    if (!(await keepWarmEnabled())) return;
    const { tenantKey } = await recapSettings();
    if (!tenantKey) return;
    const vendors = await usedInverterVendors();
    if (!vendors.length) return;
    rlog("refresh used vendors (" + reason + "):", vendors.join(", "));
    for (const v of vendors) {
      if ((await autoLoginFailsGet(v)) >= AUTOLOGIN_MAX_VENDOR_FAILS) continue;
      try { await recaptureNow(v); } catch (_) {}
    }
  }
  self.__soRefreshUsed = () => kickRefreshUsedVendors("manual");
  // NETWORK-RESTORED: reconcile connectivity. Sets a persisted wasOffline flag while down,
  // and on the false→true transition fires a governed refresh (data may have gone stale
  // during the outage). Driven by the SW online/offline events while alive AND re-checked on
  // the 1-min reaper / keep-warm alarms to catch transitions that happened while it slept.
  async function checkConnectivityRestored() {
    let online = true;
    try { online = (typeof navigator !== "undefined") ? navigator.onLine !== false : true; } catch (_) { online = true; }
    const wasOffline = (await chrome.storage.local.get(WAS_OFFLINE_KEY))[WAS_OFFLINE_KEY] === true;
    if (!online) { if (!wasOffline) { try { await chrome.storage.local.set({ [WAS_OFFLINE_KEY]: true }); } catch (_) {} } return; }
    if (wasOffline) {
      try { await chrome.storage.local.set({ [WAS_OFFLINE_KEY]: false }); } catch (_) {}
      rlog("connectivity RESTORED → refreshing used vendors");
      await kickRefreshUsedVendors("network-restored");
    }
  }
  try {
    self.addEventListener("offline", () => { try { chrome.storage.local.set({ [WAS_OFFLINE_KEY]: true }); } catch (_) {} });
    self.addEventListener("online", () => { checkConnectivityRestored().catch(() => {}); });
  } catch (_) {}
  // DASHBOARD-OPEN: when an Array Operator / NEPOOL tab becomes active or finishes loading,
  // refresh used vendors (debounced) so the owner sees live data the instant they look.
  async function onDashboardActive(url) {
    if (!_isAppUrl(url)) return;
    if (Date.now() - _lastDashKickAt < _DASH_DEBOUNCE_MS) return;
    _lastDashKickAt = Date.now();
    rlog("dashboard active → refreshing used vendors");
    await kickRefreshUsedVendors("dashboard-open");
  }
  chrome.tabs.onActivated.addListener(({ tabId }) => {
    try { chrome.tabs.get(tabId, (t) => { if (!chrome.runtime.lastError && t && t.url) onDashboardActive(t.url).catch(() => {}); }); } catch (_) {}
  });
  chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (!changeInfo || changeInfo.status !== "complete") return;
    const url = (tab && tab.url) || changeInfo.url;
    if (url && _isAppUrl(url)) onDashboardActive(url).catch(() => {});
  });
  chrome.alarms.create(KEEPWARM_ALARM, { periodInMinutes: KEEPWARM_PERIOD_MIN, delayInMinutes: 3 });

  // Run the vendors the owner actually has, one at a time (a single background tab
  // at a time keeps it invisible and cheap). After the first cycle we only refresh
  // vendors that have captured before — never open portals the owner doesn't use.
  async function runRecaptureCycle() {
    const { tenantKey } = await recapSettings();
    if (!tenantKey) { rlog("no tenant key — owner not connected; skip"); return; }
    const s = await chrome.storage.local.get(LAST_KEY);
    const known = s[LAST_KEY] || {};
    // CHINT is excluded from ALL silent recapture: its per-inverter data only loads
    // after the owner CLICKS into a site (/api/asset/site/busTypeDevices), so a
    // silently-opened background tab never captures it — every silent tick failed and
    // the tab orphaned (MV3 kills the close-watchdog), piling up. Chint refreshes only
    // on a foreground portal open. Fronius/SMA (whose data loads on the dashboard) ride
    // hourly here, unless their own live-mode alarm already drives them.
    const _fsLiveOn = { fronius: !!((await liveGet("fronius")) || {}).on, sma: !!((await liveGet("sma")) || {}).on };
    const vendors = Object.keys(RECAP_VENDORS).filter(
      (v) => (Object.keys(known).length === 0 || known[v])
        && v !== "chint" && !_fsLiveOn[v]
    );
    if (!vendors.length) return;
    if (_liveBusy) { rlog("cycle: recapture in flight (sync lock) — skip"); return; }
    _liveBusy = true;
    try {
      const st = await recapGetState();
      if (st && st.running && (Date.now() - (st.startedAt || 0)) < TAB_BUDGET_MS) {
        rlog("cycle already running — skip"); return;
      }
      rlog("recapture cycle:", vendors.join(", "));
      for (const v of vendors) {
        await recaptureVendor(v);   // resolves when that vendor finishes/timeouts
      }
    } finally { _liveBusy = false; }
  }

  // Hook the existing *_CAPTURED messages: when a silent recap is in flight, POST
  // the captured sites straight to the backend and close the tab. The normal
  // page-driven flow still works untouched — we only act when STATE_KEY is set.
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || !msg.type) return;
    const okMap = { FRONIUS_CAPTURED: "fronius", SMA_CAPTURED: "sma", CHINT_CAPTURED: "chint" };
    const failMap = { FRONIUS_CAPTURE_FAILED: "fronius", SMA_CAPTURE_FAILED: "sma", CHINT_CAPTURE_FAILED: "chint" };
    (async () => {
      const st = await recapGetState();
      if (!st || !st.running) return;            // no silent recap in flight → ignore
      // Guard against a stale state (e.g. a click-recovery he never completed): only
      // honor captures within 30 min of arming, then self-clear.
      if ((Date.now() - (st.startedAt || 0)) > 30 * 60 * 1000) { await recapClearState(); return; }
      if (okMap[msg.type] && okMap[msg.type] === st.vendor) {
        const sites = (msg.payload && Array.isArray(msg.payload.sites)) ? msg.payload.sites : [];
        const ok = await recapPost(st.vendor, sites);
        // The Chint walk emits PROGRESSIVELY as it steps through each site — save every
        // batch, but only CLOSE the surface once the walk has visited ALL sites
        // (payload.walkComplete), else a multi-site owner is truncated at site 1. The
        // recaptureVendor budget watchdog is the backstop if the walk never signals done.
        const holdForWalk = st.vendor === "chint" && !(msg.payload && msg.payload.walkComplete);
        if (!holdForWalk) await recapFinish(st.vendor, ok, sites);
      } else if (failMap[msg.type] && failMap[msg.type] === st.vendor) {
        await recapFinish(st.vendor, false, []);
      }
    })();
  });

  // Timer: fire the cycle every RECAP_PERIOD_MIN while Chrome runs. Alarms persist
  // across service-worker sleeps, so this keeps working without a live page.
  chrome.alarms.create(RECAP_ALARM, { periodInMinutes: RECAP_PERIOD_MIN, delayInMinutes: 2 });
  // Reaper: every minute, kill any recap tab that outlived the budget. An alarm (unlike
  // the setTimeout watchdog) WAKES the terminated MV3 worker, so orphans always get closed.
  chrome.alarms.create("recap-reaper", { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === "recap-reaper") {
      reapStaleRecapTabs(TAB_BUDGET_MS).catch(() => {});
      checkConnectivityRestored().catch(() => {});   // fast (~1 min) network-restored detection
      return;
    }
    if (alarm && alarm.name === RECAP_ALARM) {
      runRecaptureCycle().catch((e) => rlog("cycle error", e && e.message || e));
    } else if (alarm && alarm.name === KEEPWARM_ALARM) {
      checkConnectivityRestored().catch(() => {});
      runKeepWarmTick().catch((e) => rlog("keep-warm error", e && e.message || e));
    } else if (alarm && alarm.name === CHINT_LIVE_ALARM) {
      runChintLiveTick().catch((e) => rlog("chint-live error", e && e.message || e));
    } else if (alarm && alarm.name === "live-fronius") {
      runLiveTick("fronius").catch((e) => rlog("fronius-live error", e && e.message || e));
    } else if (alarm && alarm.name === "live-sma") {
      runLiveTick("sma").catch((e) => rlog("sma-live error", e && e.message || e));
    } else if (alarm && typeof alarm.name === "string" && alarm.name.startsWith(UTIL_LIVE_ALARM_PREFIX)) {
      // v1.9.97 — utility daily refresh: util-live-<code> → open that utility's portal.
      const code = alarm.name.slice(UTIL_LIVE_ALARM_PREFIX.length);
      runUtilityLiveTick(code).catch((e) => rlog("util-live error", code, e && e.message || e));
    }
  });
  // On install/update AND browser startup: turn live-mode on for the vendors the owner
  // actually uses (autoArmKnownLive — guarded, idempotent, fail-preserving), THEN kick
  // one recapture cycle so the bars freshen without waiting an hour. Auto-arm runs FIRST
  // so the 8s cycle correctly skips any vendor now in live-mode (no double-open). The
  // live alarms are staggered/jittered (delay >=1min) so they fire after this cycle and
  // don't collide with each other. onInstalled covers install + version-update;
  // onStartup covers a plain browser relaunch (persisted alarms usually survive it, but
  // this is the belt-and-suspenders re-assert).
  // ── Sync ALL vendors at once (concurrent) + Close all vendor tabs ───────────
  // Ford: one click opens every vendor portal in the BACKGROUND simultaneously; each
  // captures on its existing session and closes itself the instant its data lands.
  // Runs ALONGSIDE — not through — the single-surface recapture state machine (which is
  // one-at-a-time): we open our own tabs, track them, and a capture-observer closes each
  // on its *_CAPTURED message, with a watchdog backstop. Fronius/SMA/SolarEdge ride this
  // bare background-tab path (active:false). Chint is handled separately via
  // recaptureVendor("chint") — a background tab that arms the capture intent and drives the
  // v1.9.77 programmatic per-site route walk (no click), self-deleting via recapFinish + the
  // recap-reaper — because the bare path neither arms that intent nor waits for the walk.
  const SYNC_PORTALS = {
    fronius: "https://www.solarweb.com/",
    sma: "https://ennexos.sunnyportal.com/",
    solaredge: "https://monitoring.solaredge.com/",
  };
  // Vendor portal tab patterns for Close-all (within our granted host_permissions).
  const VENDOR_TAB_PATS = [
    "https://monitoring.solaredge.com/*",
    "https://www.solarweb.com/*", "https://*.solarweb.com/*",
    "https://ennexos.sunnyportal.com/*", "https://*.sunnyportal.com/*",
    "https://monitor.chintpowersystems.com/*", "https://*.chintpowersystems.com/*",
    "https://solar.chintpower.com/*", "https://*.chintpower.com/*",
  ];
  const _syncTabs = new Map();   // tabId -> { vendor, openedAt }
  const SYNC_CAPTURED = new Set(["SOLAREDGE_CAPTURED", "FRONIUS_CAPTURED", "SMA_CAPTURED", "CHINT_CAPTURED"]);
  // A capture's tab is a HIDDEN sync surface — a background "Sync all" tab OR the in-flight
  // recap tab/popup — so broadcastToSoTabs can suppress the post-capture refocus and a
  // Sync-All never yanks the operator off their current tab. (recapGetState is in scope here.)
  self.__soIsHiddenSyncSurface = async (tabId) => {
    try {
      if (_syncTabs.has(tabId)) return true;
      const st = await recapGetState();
      return !!(st && st.running && st.tabId === tabId);
    } catch (_) { return false; }
  };

  // Observer: when a SYNC tab reports its capture, close it (after a beat so its POST
  // finishes). Observe-only — never consumes the message, so the real *_CAPTURED
  // handlers still run and persist the reading.
  const _SYNC_CAPTURED_VENDOR = { SOLAREDGE_CAPTURED: "solaredge", FRONIUS_CAPTURED: "fronius", SMA_CAPTURED: "sma", CHINT_CAPTURED: "chint" };
  chrome.runtime.onMessage.addListener((msg, sender) => {
    try {
      if (!msg || !SYNC_CAPTURED.has(msg.type)) return;
      const v = _SYNC_CAPTURED_VENDOR[msg.type];
      // ANY capture of this vendor (sync-all, dashboard-chip, single-surface, or the
      // page-driven Connect flow) means the session is valid → clear the auto-login pause
      // counter. Keyed on the message type, so it covers paths that skip recapFinish.
      if (v) autoLoginFailsSet(v, 0);
      const tabId = sender && sender.tab && sender.tab.id;
      if (tabId != null && _syncTabs.has(tabId)) {
        _syncTabs.delete(tabId);
        setTimeout(() => { try { chrome.tabs.remove(tabId, () => void chrome.runtime.lastError); } catch (_) {} }, 1800);
      }
    } catch (_) {}
  });

  async function syncAllVendors(vendors) {
    // Sync-all is an explicit "do this in the background, don't move me" action — cancel any
    // pending foreground-open return so a vendor landing mid-Sync-all can never pull focus.
    try { await chrome.storage.local.remove("so_return_tab"); } catch (_) {}
    const reqList = (Array.isArray(vendors) && vendors.length ? vendors : Object.keys(SYNC_PORTALS).concat("chint"))
      .map((v) => String(v).toLowerCase());
    const want = reqList.filter((v) => SYNC_PORTALS[v]);   // solaredge / fronius / sma
    const wantChint = reqList.includes("chint");
    // Clear a bloated cookie blob before opening each portal so a user-initiated "Sync all"
    // can't land on an ERR_HTTP2_PROTOCOL_ERROR page (no-op for vendors without a domain set).
    for (const v of want) { try { await pruneVendorCookiesIfBloated(v); } catch (_) {} }
    // PARALLEL — arm a PER-VENDOR so_sync_intent for all three portal vendors at once and open
    // their tabs concurrently, so the whole sweep finishes in ~the slowest single vendor (not
    // the sum). so_sync_intent is ADDITIVE: every portal content script checks it IN ADDITION
    // to the single so_capture_intent slot (which the single-vendor flows still own), and each
    // clears ONLY its own vendor key on success — so the three can't cannibalize each other the
    // way one shared slot did. Auto-login still fires per tab: the chrome.tabs.onUpdated SSO
    // trigger resolves the vendor via _captureTabVendor's _syncTabs branch, and the auto-login
    // path has no global single-flight, so three different-origin logins run concurrently. This
    // also fixes Fronius — its slow identifier-first WSO2 login no longer has to fit a tight
    // serial budget; it gets the full ~60s watchdog like everyone else.
    const now = Date.now();
    const syncIntent = {};
    for (const v of want) syncIntent[v] = now;
    try { await chrome.storage.local.set({ so_sync_intent: syncIntent }); } catch (_) {}

    const opened = [];
    await Promise.all(want.map((v) => new Promise((res) => {
      try {
        chrome.tabs.create({ url: SYNC_PORTALS[v], active: false }, (tab) => {
          if (!chrome.runtime.lastError && tab && typeof tab.id === "number") {
            _syncTabs.set(tab.id, { vendor: v, openedAt: Date.now() });
            opened.push(tab.id);
          }
          res();
        });
      } catch (_) { res(); }
    })));

    // Chint is NOT auto-opened here. Its monitoring SPA only renders + fetches data in a VISIBLE
    // tab — a MINIMIZED/background surface (what the old popup used) runs nothing, so the capture
    // saw an empty page and wrongly reported "no session" even when the owner was signed in
    // (confirmed live: a normal tab captures every inverter; the minimized window captures zero).
    // A plain background tab DOES render but its SPA yanks itself to the foreground during the
    // route walk (focus-steal). So Chint is CLICK-TO-CONNECT: the page shows "Sign in to add
    // Chint", and the click opens monitor.chintpowersystems.com in a FOREGROUND tab
    // (OPEN_UTILITY_PORTAL — Chint is not in the cookie-wipe list, so an existing login rides),
    // where the walk reliably captures, then so_return_tab brings the owner back here.

    // Generous ~60s watchdog: close any sync tab that never captured (lapsed session / slow SSO
    // re-login), then drop so_sync_intent so a stale ts can't make a later single-vendor visit
    // auto-scrape unexpectedly (the per-vendor TTL bounds it anyway). Captured tabs self-close
    // earlier via the SYNC_CAPTURED observer.
    const ids = opened.slice();
    setTimeout(async () => {
      for (const id of ids) {
        if (_syncTabs.has(id)) { _syncTabs.delete(id); try { chrome.tabs.remove(id, () => void chrome.runtime.lastError); } catch (_) {} }
      }
      try { await chrome.storage.local.remove("so_sync_intent"); } catch (_) {}
    }, 110 * 1000);   // room for a lapsed-session SSO re-login (esp. SMA) before reaping a sync tab

    return { ok: true, opened: opened.length, vendors: want.slice() };   // Chint is click-to-connect, not opened here
  }

  function closeAllVendorTabs() {
    return new Promise((resolve) => {
      try {
        chrome.tabs.query({ url: VENDOR_TAB_PATS }, (tabs) => {
          if (chrome.runtime.lastError || !Array.isArray(tabs)) return resolve({ ok: true, closed: 0 });
          const ids = tabs.map((t) => t && t.id).filter((id) => typeof id === "number");
          ids.forEach((id) => _syncTabs.delete(id));
          if (ids.length) { try { chrome.tabs.remove(ids, () => void chrome.runtime.lastError); } catch (_) {} }
          resolve({ ok: true, closed: ids.length });
        });
      } catch (e) { resolve({ ok: false, error: String(e && e.message || e) }); }
    });
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== "SYNC_ALL_VENDORS") return;
    syncAllVendors(msg.vendors).then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;   // async sendResponse
  });
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== "CLOSE_ALL_VENDOR_TABS") return;
    closeAllVendorTabs().then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;   // async sendResponse
  });

  async function _onWake() {
    try { await reapTrackedRecapTabs(); } catch (_) {}   // close any recap tab orphaned before this wake
    try { await migrateChintBackgroundOnce(); } catch (_) {}   // v1.9.81: arm Chint bg once for existing Chint users
    try { await autoArmKnownLive(); } catch (_) {}
    setTimeout(() => runRecaptureCycle().catch(() => {}), 8000);
  }
  // onInstalled (install + version-update, incl. an unpacked reload) ALSO sweeps the
  // existing pile of orphaned vendor tabs the pre-reaper builds left behind.
  chrome.runtime.onInstalled.addListener(() => { reapLegacyVendorTabs(); _onWake(); });
  chrome.runtime.onStartup.addListener(() => { _onWake(); });
})();
