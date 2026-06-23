// background.js — service worker.
// Receives captured tokens from content.js (GMP) and vec_content.js (VEC),
// persists locally, and POSTs to the EnergyAgent API.

// v1.9.33: client-side encrypted credential vault for portal auto-login (SoVault).
// Loaded first so it's available to all handlers. Creds are AES-GCM encrypted and
// stored ONLY in chrome.storage.local — never sent to our backend.
try { importScripts("vault.js"); } catch (e) { console.warn("[EnergyAgent] vault load failed", e); }

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
  // v1.9.11: Chint / CPS (solar.chintpower.com, a Fomware white-label) per-inverter
  // capture for Array Operator. CHINT publishes no owner API key, so chint_content.js
  // reads the owner's live readings from the logged-in portal and we hand them to the
  // AO page via SO_CAPTURE_LANDED (same shape as Fronius/SMA). NOTE: the chint
  // extraction endpoints are not yet grounded against a live account — the content
  // script fails gracefully (CHINT_CAPTURE_FAILED) until they're verified.
  if (msg.type === "CHINT_CAPTURED") {
    const p = msg.payload || {};
    const landed = {
      type: "SO_CAPTURE_LANDED",
      ok: true,
      provider: "chint",
      sites: Array.isArray(p.sites) ? p.sites : [],
      accountCount: Array.isArray(p.sites) ? p.sites.length : 0,
      at: new Date().toISOString(),
    };
    broadcastToSoTabs(landed);
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
    broadcastToSoTabs(landed);
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
    broadcastToSoTabs(landed);
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

// ── Manual verification hook (v1.9.49) ───────────────────────────────────────
// Force a silent GMP refresh RIGHT NOW instead of waiting for the 12h alarm — and
// bypass the <8-day-to-expiry gate + once-a-day throttle so you can test on demand.
// HOW TO USE: chrome://extensions → EnergyAgent → "service worker" (Inspect) → in
// that DevTools Console run:  soTestGmpRefresh()
// Prereq: log into greenmountainpower.com once with this extension loaded so a GMP
// session is known. Watch for a background GMP tab to flash open ~80s, then the
// console prints whether the token's expiry advanced (= silent refresh works).
self.soTestGmpRefresh = async function () {
  const before = (await chrome.storage.local.get(STORAGE_KEYS.LAST_PAYLOAD))[STORAGE_KEYS.LAST_PAYLOAD];
  console.log("[EnergyAgent/test] GMP capture before:", before);
  if (!before || !before.tokenExpires) {
    console.log("[EnergyAgent/test] No GMP session known yet — log into greenmountainpower.com once with this extension loaded, then re-run soTestGmpRefresh().");
    return "no-session";
  }
  const prevExp = new Date(before.tokenExpires).getTime();
  const prevHash = before.tokenHash || null;
  console.log("[EnergyAgent/test] opening GMP in a BACKGROUND tab to re-capture (~80s, no sign-in expected)…");
  await silentGmpRecapture(prevExp);
  const after = (await chrome.storage.local.get(STORAGE_KEYS.LAST_PAYLOAD))[STORAGE_KEYS.LAST_PAYLOAD];
  const newExp = after && after.tokenExpires ? new Date(after.tokenExpires).getTime() : 0;
  const changed = !!after && (after.tokenHash !== prevHash || newExp > prevExp + 1000);
  if (changed) {
    console.log("[EnergyAgent/test] ✅ SILENT REFRESH WORKS — a fresh token was captured in the background, no sign-in. expires now:", after.tokenExpires);
  } else {
    console.log("[EnergyAgent/test] ℹ️ token unchanged. EITHER your token is still fresh so GMP didn't reissue it (expected right after login — re-test when it's nearer the ~21-day expiry), OR your browser GMP session has lapsed (open greenmountainpower.com in a normal tab; if it shows the login page, sign in once, then re-run). before/after expiry:", before.tokenExpires, "->", after && after.tokenExpires);
  }
  return changed ? "refreshed" : "unchanged";
};

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
  const RECAP_ALARM = "inverter-recapture";
  const RECAP_PERIOD_MIN = 60;           // hourly while the browser is running
  const TAB_BUDGET_MS = 150 * 1000;      // up to 2.5min — room for an auto-login + re-poll + capture
  const NUDGE_KEY = "so_recap_nudges";   // { fronius:"YYYY-MM-DD", ... } 1 nudge/vendor/day
  const STATE_KEY = "so_recap_state";    // { running, vendor, tabId, startedAt }
  const LAST_KEY = "so_recap_last";      // { fronius:{at,ok,sites}, ... } diagnostics

  function rlog(...a) { try { console.log("[EnergyAgent/recap]", ...a); } catch (_) {} }

  // ── CHINT live mode (v1.9.53) ────────────────────────────────────────────────
  // A fast 4-min cadence layered on THIS background-tab machinery so Chint live
  // power tracks the portal instead of freezing at the last manual capture. Opt-in:
  // armed only by an explicit AO "Connect Chint" click. Reuses recaptureVendor /
  // recapPost / recapFinish + the so_recap_state single-flight, so it can never race
  // the hourly cycle. Degrades to an honest one/day reconnect nudge after repeated
  // dead cycles (lapsed session) — never silent staleness.
  const CHINT_LIVE_ALARM = "chint-live";
  const CHINT_LIVE_PERIOD_MIN = 4;          // ~3.75x margin inside the backend 15-min fresh window
  const CHINT_LIVE_KEY = "so_chint_live";   // { on, armedAt, lastOkAt, fails }
  const CHINT_LIVE_MAX_FAILS = 6;           // ~24 min of dead cycles → disable + nudge
  async function chintLiveGet() { const s = await chrome.storage.local.get(CHINT_LIVE_KEY); return s[CHINT_LIVE_KEY] || null; }
  async function chintLiveSet(v) { try { await chrome.storage.local.set({ [CHINT_LIVE_KEY]: v }); } catch (_) {} }
  async function armChintLive() {
    await chintLiveSet({ on: true, armedAt: Date.now(), lastOkAt: 0, fails: 0 });
    try { chrome.alarms.create(CHINT_LIVE_ALARM, { periodInMinutes: CHINT_LIVE_PERIOD_MIN, delayInMinutes: 1 }); } catch (_) {}
    rlog("chint live-mode ARMED — refresh every", CHINT_LIVE_PERIOD_MIN, "min via a background tab");
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
    const live = await chintLiveGet();
    if (!live || !live.on) { try { chrome.alarms.clear(CHINT_LIVE_ALARM, () => void chrome.runtime.lastError); } catch (_) {} return; }  // self-heal zombie alarm
    const { tenantKey } = await recapSettings();
    if (!tenantKey) { rlog("chint-live: no tenant key — skip"); return; }
    const st = await recapGetState();
    if (st && st.running && (Date.now() - (st.startedAt || 0)) < TAB_BUDGET_MS) { rlog("chint-live: recap busy — skip tick"); return; }
    const before = ((await chrome.storage.local.get(LAST_KEY))[LAST_KEY] || {}).chint || {};
    await recaptureVendor("chint", { budgetMs: 60 * 1000 });   // background tab (same path as the hourly Fronius/SMA refresh) -- no separate window, no taskbar blip; self-closes on capture (~18s) or the 60s watchdog
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
  }
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

  async function recapRecordLast(vendor, ok, sites) {
    try {
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
      const today = new Date().toISOString().slice(0, 10);
      const s = await chrome.storage.local.get(NUDGE_KEY);
      const m = s[NUDGE_KEY] || {};
      if (m[vendor] === today) return;          // already nudged today
      m[vendor] = today;
      await chrome.storage.local.set({ [NUDGE_KEY]: m });
      const label = vendor === "fronius" ? "Fronius Solar.web"
        : vendor === "sma" ? "SMA Sunny Portal" : "Chint";
      chrome.notifications.create(`recap-${vendor}-${today}`, {
        type: "basic",
        iconUrl: "icons/icon128.png",
        title: "EnergyAgent: one-tap reconnect",
        message: `Click here to open ${label} and refresh your live production — one sign-in keeps it fresh for weeks.`,
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
    await recapClearState();
  }

  // Open ONE vendor's portal in a background tab, arm the capture intent, and let
  // the existing content script do its thing. A watchdog closes the tab if the
  // capture never lands (expired session) and fires the gentle nudge.
  async function recaptureVendor(vendor, opts) {
    const url = RECAP_VENDORS[vendor];
    if (!url) return;
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
  function soFillLoginForm(username, password, vendor) {
    if (!username || !password) return "no-form";
    // Grounded per-vendor selectors (verified 2026-06-16 against the live login
    // DOMs): SMA Keycloak login.sma.energy → #username/#password; Fronius WSO2
    // login.fronius.com → #usernameUserInput/#password. These are the PRIMARY path;
    // the generic matcher below is the fallback if a portal reworks its form.
    const HINTS = {
      sma: { user: "#username", pass: "#password", btn: "button[type=\"submit\"]" },
      fronius: { user: "#usernameUserInput", pass: "#password", btn: "#login-button, [data-testid=\"login-page-continue-login-button\"], button[type=\"submit\"]" },
    };
    function vis(el) { return el && el.offsetParent !== null && !el.disabled; }

    let pw = null, user = null, btn = null;
    const hint = HINTS[vendor];
    if (hint) {
      const hp = document.querySelector(hint.pass);
      const hu = document.querySelector(hint.user);
      if (vis(hp) && vis(hu)) { pw = hp; user = hu; btn = document.querySelector(hint.btn); }
    }

    // Generic fallback (vendor-agnostic): first visible password field + nearest
    // username/email field in the same form.
    if (!pw || !user) {
      const pwFields = Array.from(document.querySelectorAll('input[type="password"]')).filter(vis);
      if (!pwFields.length) return "already-in";
      pw = pwFields[0];
      const form = pw.form || document;
      const candidates = Array.from(form.querySelectorAll(
        'input[type="text"], input[type="email"], input[name*="user" i], input[name*="email" i], input[id*="user" i], input[id*="email" i]'
      )).filter((el) => vis(el) && el.type !== "password");
      user = candidates[0] || null;
      if (!user) return "no-form";
    }

    function setVal(el, val) {
      const proto = el.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, val);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    try {
      user.focus(); setVal(user, username);
      pw.focus(); setVal(pw, password);
    } catch (_) { return "no-form"; }
    if (!btn || !vis(btn)) {
      btn = (pw.form || document).querySelector(
        'button[type="submit"], input[type="submit"], button[name*="login" i], button[id*="login" i], button[id*="signin" i]'
      );
    }
    setTimeout(() => {
      try {
        if (btn && btn.offsetParent !== null) { btn.click(); return; }
        if (pw.form && pw.form.requestSubmit) { pw.form.requestSubmit(); return; }
        pw.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
      } catch (_) {}
    }, 350);
    return "submitted";
  }

  // Once per in-flight recap, attempt auto-login when a content script reports the
  // session is gone. Guards: vault must hold creds for the vendor, auto-login must
  // be enabled (opt-out), and we only try ONCE per recap tab (so a wrong password
  // can't loop-submit and lock the account). On submit the existing content-script
  // poll rides the new session and captures normally.
  const _autoLoginTried = new Set();   // tabIds we've already tried, this SW lifetime
  async function recapTryAutoLogin(vendor, tabId, state) {
    try {
      if (state !== "login_required") return;
      if (typeof tabId !== "number") return;
      const st = await recapGetState();
      if (!st || !st.running || st.vendor !== vendor || st.tabId !== tabId) return; // only during OUR recap
      if (_autoLoginTried.has(tabId)) return;            // never resubmit on the same tab
      if (typeof SoVault === "undefined") return;
      if (!(await SoVault.isEnabled(vendor))) { rlog("auto-login opted out for", vendor); return; }
      const creds = await SoVault.get(vendor);
      if (!creds) { rlog("no stored creds for", vendor, "(one-click recovery will nudge)"); return; }
      _autoLoginTried.add(tabId);
      rlog("auto-login: filling login form for", vendor, "tab", tabId);
      const res = await chrome.scripting.executeScript({
        target: { tabId },
        func: soFillLoginForm,
        args: [creds.username, creds.password, vendor],
        world: "MAIN",   // run in page context so the portal framework sees the input events
      });
      const outcome = res && res[0] && res[0].result;
      rlog("auto-login outcome for", vendor, "=>", outcome);
      // After a submit the page navigates + the content script re-polls; the normal
      // capture path takes over. If "no-form"/"already-in", we do nothing further and
      // the watchdog/one-click recovery handles it.
    } catch (e) {
      rlog("auto-login error", vendor, e && e.message || e);
    }
  }
  // Expose for the LOGIN_STATE_DETECTED handler (outside this IIFE).
  self.__soRecapTryAutoLogin = recapTryAutoLogin;

  // Run the vendors the owner actually has, one at a time (a single background tab
  // at a time keeps it invisible and cheap). After the first cycle we only refresh
  // vendors that have captured before — never open portals the owner doesn't use.
  async function runRecaptureCycle() {
    const { tenantKey } = await recapSettings();
    if (!tenantKey) { rlog("no tenant key — owner not connected; skip"); return; }
    const s = await chrome.storage.local.get(LAST_KEY);
    const known = s[LAST_KEY] || {};
    // CHINT live-mode SUPERSEDES the hourly cycle for chint (its own 4-min alarm
    // drives it) — excluding it here prevents two background tabs racing the shared
    // so_recap_state. Fronius/SMA (and chint when live-mode is off) still ride hourly.
    const liveOn = !!((await chintLiveGet()) || {}).on;
    const vendors = Object.keys(RECAP_VENDORS).filter(
      (v) => (Object.keys(known).length === 0 || known[v]) && !(v === "chint" && liveOn)
    );
    if (!vendors.length) return;
    const st = await recapGetState();
    if (st && st.running && (Date.now() - (st.startedAt || 0)) < TAB_BUDGET_MS) {
      rlog("cycle already running — skip"); return;
    }
    rlog("recapture cycle:", vendors.join(", "));
    for (const v of vendors) {
      await recaptureVendor(v);   // resolves when that vendor finishes/timeouts
    }
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
        await recapFinish(st.vendor, ok, sites);
      } else if (failMap[msg.type] && failMap[msg.type] === st.vendor) {
        await recapFinish(st.vendor, false, []);
      }
    })();
  });

  // Timer: fire the cycle every RECAP_PERIOD_MIN while Chrome runs. Alarms persist
  // across service-worker sleeps, so this keeps working without a live page.
  chrome.alarms.create(RECAP_ALARM, { periodInMinutes: RECAP_PERIOD_MIN, delayInMinutes: 2 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === RECAP_ALARM) {
      runRecaptureCycle().catch((e) => rlog("cycle error", e && e.message || e));
    } else if (alarm && alarm.name === CHINT_LIVE_ALARM) {
      runChintLiveTick().catch((e) => rlog("chint-live error", e && e.message || e));
    }
  });
  // Kick one shortly after install/update so the bars freshen without waiting 3h.
  chrome.runtime.onInstalled.addListener(() => {
    setTimeout(() => runRecaptureCycle().catch(() => {}), 8000);
  });
})();
