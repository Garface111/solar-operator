// background.js — service worker.
// Receives captured tokens from content.js, persists locally, and POSTs to
// the Solar Operator API. Also schedules periodic refresh-check alarms.

const PROD_ENDPOINT = "https://web-production-49c83.up.railway.app/v1/sync";
const STORAGE_KEYS = {
  ENDPOINT: "api_endpoint",
  TENANT_KEY: "tenant_key",
  LAST_SYNC: "last_sync",
  LAST_PAYLOAD: "last_payload",
  LAST_ERROR: "last_error",
};

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

  const res = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text().catch(() => "")}`);
  }
  return res.json().catch(() => ({}));
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "GMP_TOKEN_CAPTURED") {
    (async () => {
      try {
        await chrome.storage.local.set({
          [STORAGE_KEYS.LAST_PAYLOAD]: {
            capturedAt: msg.payload.capturedAt,
            accountCount: msg.payload.accounts.length,
            username: msg.payload.user.username,
            tokenExpires: msg.payload.auth.apiTokenExpires,
            tokenHash: msg.tokenHash,
          },
        });

        const settings = await getSettings();
        if (!settings.tenantKey) {
          // No tenant key configured — capture-only mode. Useful during MVP.
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

        const result = await postSync(msg.payload);
        await chrome.storage.local.set({
          [STORAGE_KEYS.LAST_SYNC]: {
            ok: true,
            at: new Date().toISOString(),
            endpoint: settings.endpoint,
            result,
          },
          [STORAGE_KEYS.LAST_ERROR]: null,
        });
        chrome.action.setBadgeText({ text: "✓" });
        chrome.action.setBadgeBackgroundColor({ color: "#2e6b3a" });
        sendResponse({ ok: true, endpoint: settings.endpoint });
      } catch (e) {
        await chrome.storage.local.set({
          [STORAGE_KEYS.LAST_ERROR]: { at: new Date().toISOString(), message: String(e) },
        });
        chrome.action.setBadgeText({ text: "!" });
        chrome.action.setBadgeBackgroundColor({ color: "#c97a3d" });
        sendResponse({ ok: false, error: String(e) });
      }
    })();
    return true; // keep channel open for async sendResponse
  }
});

// Expiry check — nudge the user before the JWT runs out.
chrome.alarms.create("token-expiry-check", { periodInMinutes: 60 * 12 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
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

chrome.runtime.onInstalled.addListener(() => {
  chrome.action.setBadgeText({ text: "" });
});
