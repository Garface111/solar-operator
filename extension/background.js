// background.js — service worker.
// Receives captured tokens from content.js (GMP) and vec_content.js (VEC),
// persists locally, and POSTs to the EnergyAgent API.

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
function broadcastToSoTabs(message) {
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
  } catch (e) {
    console.warn("[EnergyAgent] broadcastToSoTabs failed:", e);
  }
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
    broadcastToSoTabs(landed);
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
    broadcastToSoTabs(landed);
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
    broadcastToSoTabs(landed);
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
  // its API (uiapi.sunnyportal.com) are DIFFERENT origins, and uiapi answers with
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
        }
        // v1.9.2: same for SMA ennexOS (Sunny Portal). Not in the wipe list —
        // ride the owner's existing session.
        if (host.endsWith("sunnyportal.com")) {
          try {
            await chrome.storage.local.set({
              so_capture_intent: { vendor: "sma", ts: Date.now() },
            });
          } catch (_) { /* non-fatal */ }
        }
      } catch (e) {
        // Cookie wipe is best-effort; opening the tab still proceeds.
        console.warn("[so] cookie wipe failed", e);
      }
      chrome.tabs.create({ url, active }, (tab) => {
        if (chrome.runtime.lastError) {
          sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        } else {
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
    if (!allowed.some((d) => domain.endsWith(d))) {
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
        if (msg.endpoint && typeof msg.endpoint === "string") {
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
      } catch (e) {
        console.warn("[EnergyAgent] LOGIN_STATE_DETECTED handling failed:", e);
      }
    })();
    return false;
  }
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

// Expiry check — nudge the user before the JWT runs out (GMP only).
chrome.alarms.create("token-expiry-check", { periodInMinutes: 60 * 12 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "heartbeat") {
    await sendHeartbeat();
    return;
  }
  if (alarm.name !== "token-expiry-check") return;
  const s = await chrome.storage.local.get(STORAGE_KEYS.LAST_PAYLOAD);
  const last = s[STORAGE_KEYS.LAST_PAYLOAD];
  if (!last?.tokenExpires) return;
  const msLeft = new Date(last.tokenExpires).getTime() - Date.now();
  const daysLeft = msLeft / (1000 * 60 * 60 * 24);
  if (daysLeft < 3 && daysLeft > 0) {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: "EnergyAgent: reconnect needed",
      message: `Your GMP session expires in ${Math.ceil(daysLeft)} day(s). Log in to greenmountainpower.com to refresh.`,
    });
  }
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
