// so_bridge.js — page ↔ extension bridge for the Solar Operator SPA.
//
// Runs on solaroperator.org + the Railway origin. The SPA cannot call
// chrome.tabs.create directly (no chrome.* in page context), so it
// window.postMessage's an intent and we forward it via chrome.runtime.
//
// Protocol — page → bridge:
//   window.postMessage({ type: "SO_OPEN_PORTAL", url: "https://...", reqId: "..." }, "*")
// Bridge → page (ack):
//   window.postMessage({ type: "SO_OPEN_PORTAL_ACK", reqId, ok: true|false }, "*")
//
// The SPA uses the ack to decide between "extension opened it in a
// background tab" and "fall back to window.open in a foreground tab".
//
// We also broadcast a one-shot SO_EXTENSION_PRESENT on load so the SPA
// can detect the extension synchronously (used by the onboarding banner).
(() => {
  try {
    window.postMessage({ type: "SO_EXTENSION_PRESENT", version: chrome.runtime.getManifest().version }, "*");
  } catch (_) { /* manifest unavailable in odd contexts — non-fatal */ }

  window.addEventListener("message", (event) => {
    // Only accept messages from the same window (page → bridge).
    if (event.source !== window) return;
    const data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type !== "SO_OPEN_PORTAL") return;

    const reqId = data.reqId || null;
    const url = String(data.url || "").trim();
    if (!url || !/^https:\/\//i.test(url)) {
      window.postMessage({ type: "SO_OPEN_PORTAL_ACK", reqId, ok: false, error: "invalid-url" }, "*");
      return;
    }

    chrome.runtime.sendMessage({ type: "OPEN_UTILITY_PORTAL", url }, (resp) => {
      const ok = !chrome.runtime.lastError && resp && resp.ok;
      window.postMessage({
        type: "SO_OPEN_PORTAL_ACK",
        reqId,
        ok: !!ok,
        error: chrome.runtime.lastError ? chrome.runtime.lastError.message : (resp && resp.error) || null,
      }, "*");
    });
  });
})();
