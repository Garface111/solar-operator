// so_bridge.js — page ↔ extension bridge for the EnergyAgent SPA.
//
// Runs on nepooloperator.com (+ solaroperator.org during transition) and the Railway
// origin. The SPA cannot call
// chrome.* directly (no chrome.* in page context), so it window.postMessage's
// intents and we forward via chrome.runtime; broadcasts coming back from
// background.js are reposted to the page so React effects can react live.
//
// ──────────────────────────────────────────────────────────────────────
// PROTOCOL (see extension/BRIDGE_PROTOCOL.md for the canonical spec)
// ──────────────────────────────────────────────────────────────────────
//
// Page → bridge (request, ack-driven):
//   SO_OPEN_PORTAL      { url, reqId }              → SO_OPEN_PORTAL_ACK { reqId, ok, error? }
//   SO_PAIR             { tenantKey, endpoint?, reqId } → SO_PAIR_ACK   { reqId, ok, version, lastSyncAt?, error? }
//   SO_STATUS_REQUEST   { reqId }                   → SO_STATUS_ACK     { reqId, ok, version, tenantKeySet, lastSyncAt?, lastPayload?, loginState? }
//
// Bridge → page (one-shot broadcasts, no reqId):
//   SO_EXTENSION_PRESENT  { version }
//   SO_LOGIN_STATE        { provider, state, url, at }
//   SO_CAPTURE_LANDED     { ok, provider, accountCount, at, error? }

(() => {
  // ── Announce presence so the SPA can detect us synchronously. ───────
  try {
    window.postMessage({
      type: "SO_EXTENSION_PRESENT",
      version: chrome.runtime.getManifest().version,
    }, "*");
  } catch (_) { /* manifest unavailable in odd contexts — non-fatal */ }

  // ── Page → bridge → background ──────────────────────────────────────
  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || typeof data !== "object") return;

    if (data.type === "SO_OPEN_PORTAL") {
      const reqId = data.reqId || null;
      const url = String(data.url || "").trim();
      const active = data.active === true;
      // Forward the provider/vendor hint so background.js can arm the matching
      // capture intent (e.g. vec vs wec on a shared smarthub.coop host).
      const provider = (data.provider || data.vendor || "") + "";
      if (!url || !/^https:\/\//i.test(url)) {
        window.postMessage({ type: "SO_OPEN_PORTAL_ACK", reqId, ok: false, error: "invalid-url" }, "*");
        return;
      }
      chrome.runtime.sendMessage({ type: "OPEN_UTILITY_PORTAL", url, active, provider, vendor: provider }, (resp) => {
        const ok = !chrome.runtime.lastError && resp && resp.ok;
        window.postMessage({
          type: "SO_OPEN_PORTAL_ACK",
          reqId,
          ok: !!ok,
          error: chrome.runtime.lastError ? chrome.runtime.lastError.message : (resp && resp.error) || null,
        }, "*");
      });
      return;
    }

    // Page asks the extension to RE-SCRAPE a vendor portal NOW (the spreadsheet view's
    // per-vendor Refresh button). Background opens a silent background tab, captures
    // fresh power, and POSTs it to the backend; we relay the result so the page can
    // refetch. SO_RECAPTURE { vendor, reqId } → SO_RECAPTURE_DONE { reqId, ok, captured }.
    if (data.type === "SO_RECAPTURE") {
      const reqId = data.reqId || null;
      const vendor = String(data.vendor || "").toLowerCase();
      if (!vendor) {
        window.postMessage({ type: "SO_RECAPTURE_DONE", reqId, ok: false, error: "missing-vendor" }, "*");
        return;
      }
      chrome.runtime.sendMessage({ type: "TRIGGER_RECAPTURE", vendor }, (resp) => {
        window.postMessage({
          type: "SO_RECAPTURE_DONE",
          reqId,
          ok: !!(resp && resp.ok),
          captured: !!(resp && resp.captured),
          error: chrome.runtime.lastError ? chrome.runtime.lastError.message : (resp && resp.error) || null,
        }, "*");
      });
      return;
    }

    if (data.type === "SO_PAIR") {
      const reqId = data.reqId || null;
      const tenantKey = String(data.tenantKey || "").trim();
      const endpoint = typeof data.endpoint === "string" ? data.endpoint : undefined;
      if (!tenantKey) {
        window.postMessage({ type: "SO_PAIR_ACK", reqId, ok: false, error: "missing-tenant-key" }, "*");
        return;
      }
      chrome.runtime.sendMessage({ type: "SO_PAIR", tenantKey, endpoint }, (resp) => {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : (resp && resp.error) || null;
        window.postMessage({
          type: "SO_PAIR_ACK",
          reqId,
          ok: !!(resp && resp.ok),
          version: resp ? resp.version : undefined,
          lastSyncAt: resp ? resp.lastSyncAt : undefined,
          error: err,
        }, "*");
      });
      return;
    }

    if (data.type === "SO_STATUS_REQUEST") {
      const reqId = data.reqId || null;
      chrome.runtime.sendMessage({ type: "SO_STATUS_REQUEST" }, (resp) => {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : (resp && resp.error) || null;
        window.postMessage({
          type: "SO_STATUS_ACK",
          reqId,
          ok: !!(resp && resp.ok),
          version: resp ? resp.version : undefined,
          tenantKeySet: resp ? resp.tenantKeySet : undefined,
          lastSyncAt: resp ? resp.lastSyncAt : undefined,
          lastPayload: resp ? resp.lastPayload : undefined,
          loginState: resp ? resp.loginState : undefined,
          error: err,
        }, "*");
      });
      return;
    }

    if (data.type === "SO_WIPE_COOKIES") {
      const reqId = data.reqId || null;
      const domain = String(data.domain || "");
      chrome.runtime.sendMessage({ type: "SO_WIPE_COOKIES", domain }, (resp) => {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : (resp && resp.error) || null;
        window.postMessage({
          type: "SO_WIPE_COOKIES_ACK",
          reqId,
          ok: !!(resp && resp.ok),
          wiped: resp ? resp.wiped : undefined,
          error: err,
        }, "*");
      });
      return;
    }

    // v1.9.34: auto-login vault relay. The Master Account tab manages auto-refresh
    // credentials + opt-out through these; secrets only ever go page → background to
    // be encrypted at rest, and the status reply NEVER includes a password.
    if (data.type === "SO_VAULT") {
      const reqId = data.reqId || null;
      const op = String(data.op || "");                 // status | set | clear | optout
      const msg = { type: "SO_VAULT_" + op.toUpperCase(), vendor: data.vendor };
      if (op === "set") { msg.username = data.username; msg.password = data.password; }
      if (op === "optout") { msg.optedOut = !!data.optedOut; }
      chrome.runtime.sendMessage(msg, (resp) => {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : (resp && resp.error) || null;
        window.postMessage({
          type: "SO_VAULT_ACK",
          reqId,
          op,
          ok: !!(resp && resp.ok),
          status: resp ? resp.status : undefined,   // only for op==="status"
          error: err,
        }, "*");
      });
      return;
    }
  });

  // ── Background → bridge → page broadcasts ────────────────────────────
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "SO_LOGIN_STATE" || msg.type === "SO_CAPTURE_LANDED" || msg.type === "SO_CAPTURE_FAILED") {
      window.postMessage(msg, "*");
    }
    // We don't need to keep the channel open for an async response —
    // background broadcasts are fire-and-forget.
  });
})();
