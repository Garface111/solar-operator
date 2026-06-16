// gmp_meter_content.js — runs on greenmountainpower.com.
//
// Array Operator UTILITY-METER production capture for Green Mountain Power.
//
// ── APPROACH: DIRECT AUTHENTICATED READ (token from the page, fetch via SW) ───
// Unlike Chint (which needed passive observation), GMP authenticates its API
// with a Bearer JWT the SPA stores in localStorage "gmp-vue" → .user.apitoken.
// So we can read the owner's OWN solar generation straight from the API:
//   GET https://api.greenmountainpower.com/api/v2/users/current      → energyAccounts[]
//   GET https://api.greenmountainpower.com/api/v2/usage/{acct}/summary → generation
//
// CORS: the page is greenmountainpower.com but the API is the cross-origin
// api.greenmountainpower.com — a credentialed content-script fetch can CORS-
// block. So we DON'T fetch here. We hand the JWT to background.js (service
// worker holds host_permissions for api.greenmountainpower.com) which does the
// authenticated GETs CORS-free and returns the assembled accounts. Mirrors the
// SMA_API_GET proxy pattern already in background.js.
//
// PRIVACY: capture runs ONLY when a recent, explicit AO "Connect GMP" click set
// the intent flag. SAFETY: read-only — the JWT is the owner's own session token;
// nothing persists beyond the in-memory hand-off.
//
// NO-SOLAR CASE: an account with isNetMetered=false / 0 generation is VALID — it
// just has no solar. We still emit it, marked honestly (has_generation=false),
// so the AO UI can tell the owner "this account has no solar production".

(function () {
  "use strict";
  if (!/(^|\.)greenmountainpower\.com$/.test(location.hostname)) return;

  const GMP_DEBUG = false;
  const LOG = (...a) => { if (GMP_DEBUG) { try { console.log("[EnergyAgent GMP-METER]", ...a); } catch (_) {} } };
  LOG("content script LOADED on", location.href);

  const GMP_KEY = "gmp-vue";
  const INTENT_KEY = "so_capture_intent";
  const INTENT_TTL_MS = 10 * 60 * 1000;
  const POLL_INTERVAL_MS = 5000;
  const MAX_POLLS = 24;                 // ~2 min — give the SPA time to log in
  let polls = 0;
  let done = false;
  let lastLoginState = null;
  let warnedLogin = false;

  function readJwt() {
    try {
      const raw = localStorage.getItem(GMP_KEY);
      if (!raw) return null;
      const outer = JSON.parse(raw);
      if (outer && outer.user && outer.user.apitoken) return String(outer.user.apitoken);
    } catch (e) { LOG("failed to parse gmp-vue", e); }
    return null;
  }

  function hasIntent() {
    return new Promise((res) => {
      try {
        chrome.storage.local.get(INTENT_KEY, (s) => {
          const it = s && s[INTENT_KEY];
          res(!!(it && it.vendor === "gmp" && (Date.now() - (it.ts || 0)) < INTENT_TTL_MS));
        });
      } catch (_) { res(false); }
    });
  }
  function clearIntent() { try { chrome.storage.local.remove(INTENT_KEY); } catch (_) {} }

  function broadcastLoginState(state) {
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED", provider: "gmp",
      state, url: location.href, at: new Date().toISOString(),
    }, () => void chrome.runtime.lastError);
  }

  function reportFailure(reason) {
    chrome.runtime.sendMessage(
      { type: "GMP_METER_CAPTURE_FAILED", reason: String(reason || "unknown"), url: location.href },
      () => void chrome.runtime.lastError,
    );
  }

  // Ask background.js (service worker) to do the cross-origin authenticated GETs.
  function fetchUsage(jwt) {
    return new Promise((res) => {
      try {
        chrome.runtime.sendMessage({ type: "GMP_FETCH_USAGE", jwt }, (resp) => {
          if (chrome.runtime.lastError) { res({ ok: false, error: String(chrome.runtime.lastError.message) }); return; }
          res(resp || { ok: false, error: "no response" });
        });
      } catch (e) { res({ ok: false, error: String((e && e.message) || e) }); }
    });
  }

  async function tick() {
    if (done) return;
    polls++;

    const intent = await hasIntent();
    if (!intent) {
      LOG("capture not requested from Array Operator — idle");
      return;
    }

    const jwt = readJwt();
    if (!jwt) {
      if (!warnedLogin) { LOG("no GMP JWT in localStorage — owner must sign in"); warnedLogin = true; }
      broadcastLoginState("login_required");
      return;
    }
    broadcastLoginState("signed_in");

    const resp = await fetchUsage(jwt);
    if (!resp || !resp.ok) {
      // A 401 means the JWT expired — surface as login_required, not a hard fail,
      // unless we've exhausted the window.
      const err = (resp && (resp.error || resp.status)) || "unknown";
      LOG("GMP_FETCH_USAGE failed:", err);
      if (resp && resp.status === 401) { broadcastLoginState("login_required"); return; }
      if (polls < MAX_POLLS) return;     // transient — keep polling
      reportFailure("GMP usage fetch failed: " + err);
      done = true;
      return;
    }

    const accounts = Array.isArray(resp.accounts) ? resp.accounts : [];
    if (!accounts.length) {
      if (polls < MAX_POLLS) return;     // give the API a moment
      reportFailure("no GMP energy accounts found");
      done = true;
      return;
    }

    // Assemble and ship. Accounts with no solar are kept and marked honestly by
    // the backend (has_generation=false) — we never drop them here.
    const payload = {
      provider: "gmp",
      capturedAt: new Date().toISOString(),
      accounts: accounts.map((a) => ({
        account_number: a.account_number,
        nickname: a.nickname || null,
        summary: a.summary || {},
        daily: Array.isArray(a.daily) ? a.daily : [],
      })),
    };
    LOG("EMIT payload:", payload.accounts.length, "account(s)");
    chrome.runtime.sendMessage({ type: "GMP_METER_CAPTURED", payload }, () => void chrome.runtime.lastError);
    // One clean read is enough for the summary path — stop and disarm.
    done = true;
    clearIntent();
  }

  tick();
  const iv = setInterval(() => {
    if (done) { clearInterval(iv); return; }
    if (polls >= MAX_POLLS) {
      clearInterval(iv);
      return;
    }
    tick();
  }, POLL_INTERVAL_MS);
})();
