// background.js — service worker.
// Receives captured tokens from content.js (GMP) and vec_content.js (VEC),
// persists locally, and POSTs to the Solar Operator API.

// v1.3.0: SO_PAIR / SO_STATUS_REQUEST handlers + SO_CAPTURE_LANDED +
//         SO_LOGIN_STATE broadcasts to every solaroperator.org tab so the
//         onboarding wizard can mirror live state without polling.
// v1.2.0: OPEN_UTILITY_PORTAL background-tab handler + so_bridge.js content
//         script for SPA ↔ extension postMessage.
// v1.1.0: added VEC / NISC SmartHub support (VEC_DATA_CAPTURED)
// v1.0.2: primary endpoint on api.solaroperator.org with Railway fallback
// during the CNAME transition window.
const PROD_ENDPOINT = "https://api.solaroperator.org/v1/sync";
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

// v1.3.0: broadcast a payload to every open solaroperator.org tab so the
// SPA can react without polling. The so_bridge.js content script picks
// these up via chrome.runtime.onMessage and re-posts to its window.
const SO_TAB_URLS = [
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
    console.warn("[Solar Operator] broadcastToSoTabs failed:", e);
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
      console.warn("[Solar Operator] primary endpoint failed, retrying fallback:", e.message);
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
    const capturedMsg = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: payload.provider || "gmp",
      accountCount: (payload.accounts || []).length,
      at,
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
  if (msg.type === "GMP_TOKEN_CAPTURED" || msg.type === "VEC_DATA_CAPTURED") {
    _handleSync(msg.payload, msg.tokenHash, sendResponse);
    return true; // keep channel open for async sendResponse
  }
  // v1.2.0: SPA asks the extension to open the utility portal in a
  // background tab — gives content.js a chance to capture without
  // stealing the operator's focus from solaroperator.org.
  if (msg.type === "OPEN_UTILITY_PORTAL") {
    const url = String(msg.url || "");
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
      } catch (e) {
        // Cookie wipe is best-effort; opening the tab still proceeds.
        console.warn("[so] cookie wipe failed", e);
      }
      chrome.tabs.create({ url, active: false }, (tab) => {
        if (chrome.runtime.lastError) {
          sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          sendResponse({ ok: true, tabId: tab && tab.id });
        }
      });
    })();
    return true; // async sendResponse
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
        console.warn("[Solar Operator] LOGIN_STATE_DETECTED handling failed:", e);
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
      title: "Solar Operator: reconnect needed",
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
      console.log("[Solar Operator] v1.0.2 migration: cleared stale Railway endpoint, now defaulting to", PROD_ENDPOINT);
    }
  }
});
