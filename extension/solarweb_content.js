// solarweb_content.js — runs on www.solarweb.com (Fronius Solar.web portal).
//
// Array Operator inverter capture for FRONIUS. Unlike SolarEdge (where we read a
// durable API key and let the backend pull via the official API), Fronius's
// Solar.web Query API is a PAID business API NOT offered in the USA — so there
// is no usable key to capture. Instead, this content script reads the owner's
// LIVE READINGS straight from the logged-in portal's own JSON endpoints (using
// the page's session cookies) and ships those readings to the backend ingest
// path. The browser is the only thing that can see this data without a paid key.
//
// Grounded against a LIVE Solar.web account 2026-06-15 (HAR capture, account
// with systems "Waterford" 12 inverters + "west chester" 20 inverters). Every
// endpoint below was verified live; all are session-cookie authed (a content
// script's fetch() sends first-party cookies with credentials:"include").
//
// PRIVACY: capture runs ONLY when a recent, explicit AO "Connect Fronius" click
// set the intent flag — we never read on a casual visit. SAFETY: read-only GETs;
// the extension persists nothing beyond the in-memory hand-off.
//
// VERIFIED ENDPOINTS (all GET, JSON, www.solarweb.com, credentials:"include"):
//   /PvSystems/GetPvSystemsForListView?_=<ts>
//     -> { data: [ { PvSystemId, PvSystemName, EnergyTodayInkWh, InverterCount,
//                    KwhPerKwp, LastImport:"/Date(ms)/", LastImportDisp,
//                    ErrorCntToday, OnlineStatus } ] }
//   /ActualData/GetActualValues?withOnlineState=True&_=<ts>
//     -> [ { PvSystemId, TotalPower (WATTS), DalosOnline:[..], DalosOffline:[..] } ]
//   /Messages/GetUnreadMessageCountForUser?_=<ts>   (200 when signed in)

(function () {
  "use strict";
  if (!/(^|\.)solarweb\.com$/.test(location.hostname)) return;

  const INTENT_KEY = "so_capture_intent";   // {vendor, ts} set by background on SO_OPEN_PORTAL
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
  // Solar.web serializes dates as ASP.NET "/Date(1781542800000)/". Parse to ISO.
  function parseAspNetDate(s) {
    if (typeof s !== "string") return null;
    const m = s.match(/\/Date\((\d+)\)\//);
    if (!m) return null;
    try { return new Date(Number(m[1])).toISOString(); } catch (_) { return null; }
  }
  // 200 on the lightweight per-user message count = signed in; 401/redirect = not.
  async function isSignedIn() {
    try {
      const r = await fetch("/Messages/GetUnreadMessageCountForUser?_=" + Date.now(),
        { credentials: "include" });
      return r.ok;
    } catch (_) { return false; }
  }
  function broadcastLoginState(state) {
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED", provider: "fronius",
      state, url: location.href, at: new Date().toISOString(),
    }, () => void chrome.runtime.lastError);
  }

  // Derive nameplate kWp from today's energy ÷ specific yield (kWh/kWp). Only
  // valid when both are present and the day has meaningful production, so this
  // is a best-effort hint — the backend/engine treats peak_power_kw as optional
  // and infers from observed peak when it's null.
  function deriveNameplateKw(energyTodayKwh, kwhPerKwp) {
    const e = Number(energyTodayKwh);
    const y = Number(kwhPerKwp);
    if (!isFinite(e) || !isFinite(y) || y <= 0 || e <= 0) return null;
    return Math.round((e / y) * 10) / 10;
  }
  // Map live power + online/error signals to a plain status the engine understands.
  function deriveStatus(powerW, errorCnt, online) {
    if (online === false) return "offline";
    if (Number(errorCnt) > 0) return "fault";
    if (Number(powerW) > 0) return "producing";
    return "idle"; // online, no error, zero power (night / not producing)
  }

  async function captureFlow() {
    // 1. System list — names, inverter counts, today's energy, specific yield.
    const listResp = await getJson("/PvSystems/GetPvSystemsForListView?_=" + Date.now());
    const systems = (listResp && listResp.data) || [];
    if (!systems.length) throw new Error("no pv systems");

    // 2. Live values — current AC power (WATTS) + online state, keyed by PvSystemId.
    let liveArr = [];
    try {
      liveArr = await getJson("/ActualData/GetActualValues?withOnlineState=True&_=" + Date.now());
    } catch (_) { liveArr = []; }    // live is a bonus; the list alone is still useful
    const liveMap = {};
    for (const lv of (liveArr || [])) {
      if (lv && lv.PvSystemId) {
        liveMap[lv.PvSystemId] = {
          power_w: typeof lv.TotalPower === "number" ? lv.TotalPower : null,
          online: Array.isArray(lv.DalosOnline) ? lv.DalosOnline.length > 0 : null,
        };
      }
    }

    const sites = systems.map((s) => {
      const live = liveMap[s.PvSystemId] || {};
      const energyToday = typeof s.EnergyTodayInkWh === "number" ? s.EnergyTodayInkWh : null;
      return {
        site_id: s.PvSystemId,
        name: s.PvSystemName || null,
        peak_power_kw: deriveNameplateKw(energyToday, s.KwhPerKwp),
        inverter_count: typeof s.InverterCount === "number" ? s.InverterCount : null,
        energy_today_kwh: energyToday,
        kwh_per_kwp: typeof s.KwhPerKwp === "number" ? s.KwhPerKwp : null,
        current_power_w: live.power_w != null ? live.power_w : null,
        error_count_today: typeof s.ErrorCntToday === "number" ? s.ErrorCntToday : 0,
        online: live.online,
        status: deriveStatus(live.power_w, s.ErrorCntToday, live.online),
        last_report: parseAspNetDate(s.LastImport) || null,
        last_report_disp: s.LastImportDisp || null,
      };
    });

    return {
      provider: "fronius",
      capturedAt: new Date().toISOString(),
      sites,
    };
  }

  function hasIntent() {
    return new Promise((res) => {
      try {
        chrome.storage.local.get(INTENT_KEY, (s) => {
          const it = s && s[INTENT_KEY];
          res(!!(it && it.vendor === "fronius" && (Date.now() - (it.ts || 0)) < INTENT_TTL_MS));
        });
      } catch (_) { res(false); }
    });
  }
  function clearIntent() { try { chrome.storage.local.remove(INTENT_KEY); } catch (_) {} }

  async function tick() {
    if (done) return;
    polls++;
    if (!(await hasIntent())) return;            // no explicit AO click → never capture
    if (!(await isSignedIn())) { broadcastLoginState("login_required"); return; }
    broadcastLoginState("signed_in");
    let payload;
    try { payload = await captureFlow(); } catch (_) { return; }   // retry on next tick
    if (!(payload.sites || []).length) return;   // nothing usable yet
    // De-dupe identical captures (e.g. the poller firing twice) by hashing the
    // site ids + their today-energy snapshot.
    const sig = (payload.sites || []).map((s) => s.site_id + ":" + s.energy_today_kwh).join("|");
    const h = await hashString(sig);
    if (h === lastHash) return;
    lastHash = h;
    done = true;
    clearIntent();
    chrome.runtime.sendMessage({ type: "FRONIUS_CAPTURED", payload }, () => void chrome.runtime.lastError);
  }

  tick();
  const iv = setInterval(() => {
    if (done || polls >= MAX_POLLS) { clearInterval(iv); return; }
    tick();
  }, POLL_INTERVAL_MS);
})();
