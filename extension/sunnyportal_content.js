// sunnyportal_content.js — runs on ennexos.sunnyportal.com (SMA's ennexOS portal).
//
// Array Operator PER-INVERTER capture for SMA. SMA's official Monitoring API
// (smaapis.de) needs a developer-app registration + per-owner OAuth consent —
// high friction. Instead this content script reads the owner's live per-inverter
// readings straight from the logged-in ennexOS portal (its uiapi.sunnyportal.com
// backend, session-cookie authed) and ships them to the backend ingest path,
// exactly like the Fronius (Solar.web) path.
//
// Grounded against a LIVE ennexOS account 2026-06-15 (HAR capture, plant 8296660
// "Timberworks" / Green Mountain Community Solar — 7 STP inverters + 1 datamanager).
// Every endpoint below was verified live; all are session-cookie authed (a
// content script fetch() sends first-party cookies with credentials:"include").
//
// PRIVACY: capture runs ONLY when a recent, explicit AO "Connect SMA" click set
// the intent flag. SAFETY: read-only GETs; the extension persists nothing.
//
// VERIFIED ENDPOINTS (all on uiapi.sunnyportal.com, GET, JSON, credentials:include):
//   /api/v1/navigation/menuitems  -> { componentType:"Plant", componentId, name }  (the plant id)
//   /api/v1/plants/{plantId}      -> { plantId, plantOperator:{...} }               (plant name/owner)
//   /api/v1/overview/{plantId}/devices -> [ { serial, product (e.g. "STP 24kTL-US-10"),
//       name (e.g. "#4 24kW 191245395"), componentId, pvPower (live W),
//       totWhOutToday (Wh), totWhOutYesterday (Wh), state (307=ok),
//       inverterComparisonState, componentType:"Device" } ]
//   Plant id is also in the SPA URL path: ennexos.sunnyportal.com/<plantId>/monitoring/...

(function () {
  "use strict";
  if (!/(^|\.)sunnyportal\.com$/.test(location.hostname)) return;

  const UIAPI = "https://uiapi.sunnyportal.com";
  const INTENT_KEY = "so_capture_intent";
  const INTENT_TTL_MS = 10 * 60 * 1000;
  const POLL_INTERVAL_MS = 4000;
  const MAX_POLLS = 30;
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
  // 200 on the lightweight menu call = signed in; 401/403 = not.
  async function isSignedIn() {
    try {
      const r = await fetch(UIAPI + "/api/v1/navigation/menuitems", { credentials: "include" });
      return r.ok;
    } catch (_) { return false; }
  }
  function broadcastLoginState(state) {
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED", provider: "sma",
      state, url: location.href, at: new Date().toISOString(),
    }, () => void chrome.runtime.lastError);
  }

  // SMA product string -> nameplate kW. "STP 24kTL-US-10" -> 24, falls back to
  // the device name "#4 24kW 191245395".
  function nameplateKw(product, name) {
    const m = (String(product || "").match(/STP\s*(\d+(?:\.\d+)?)\s*k/i))
      || (String(name || "").match(/(\d+(?:\.\d+)?)\s*k[wW]/));
    return m ? Math.round(parseFloat(m[1]) * 10) / 10 : null;
  }
  // ennexOS device "state" 307 = OK/feeding. Anything else we surface as a fault
  // signal; a zero-energy producing-hours inverter the peer engine flags "dead".
  function deriveStatus(d) {
    const p = Number(d.pvPower);
    if (d.state != null && d.state !== 307) return "fault";
    if (p > 0) return "producing";
    return "idle";
  }

  // Find the plant id: prefer the SPA URL path (/<plantId>/...), else the
  // navigation menu's Plant componentId.
  async function resolvePlantId() {
    const m = location.pathname.match(/\/(\d{4,})\b/);
    if (m) return m[1];
    try {
      const nav = await getJson(UIAPI + "/api/v1/navigation/menuitems");
      if (nav && nav.componentType === "Plant" && nav.componentId) return String(nav.componentId);
    } catch (_) { /* fall through */ }
    return null;
  }

  async function captureFlow() {
    const plantId = await resolvePlantId();
    if (!plantId) throw new Error("no plant id");

    let plantName = null;
    try {
      const plant = await getJson(UIAPI + "/api/v1/plants/" + plantId);
      plantName = (plant && plant.name) || null;   // plants/{id} carries no name field reliably
    } catch (_) { /* name is a bonus */ }
    // The navigation menu reliably carries the plant display name ("Timberworks").
    if (!plantName) {
      try {
        const nav = await getJson(UIAPI + "/api/v1/navigation/menuitems");
        if (nav && nav.name) plantName = nav.name;
      } catch (_) {}
    }

    // Per-inverter overview — the comb. One call, every device with live power +
    // today's energy directly (no curve integration needed).
    const devices = await getJson(UIAPI + "/api/v1/overview/" + plantId + "/devices");
    const inverters = (devices || [])
      .filter((d) => d && d.componentType === "Device" && d.pvPower !== null && d.pvPower !== undefined)
      .map((d) => ({
        serial: String(d.serial || d.componentId),
        name: d.name || String(d.serial),
        model: d.product || null,
        nameplate_kw: nameplateKw(d.product, d.name),
        energy_today_kwh: typeof d.totWhOutToday === "number" ? d.totWhOutToday / 1000.0 : null,
        current_power_w: typeof d.pvPower === "number" ? d.pvPower : null,
        status: deriveStatus(d),
      }));

    if (!inverters.length) throw new Error("no producing inverters");

    // System totals = sum of the inverters (ennexOS gives per-device Wh directly).
    let energyToday = 0, liveW = 0, peakKw = 0;
    inverters.forEach((iv) => {
      if (iv.energy_today_kwh) energyToday += iv.energy_today_kwh;
      if (iv.current_power_w) liveW += iv.current_power_w;
      if (iv.nameplate_kw) peakKw += iv.nameplate_kw;
    });

    const site = {
      site_id: String(plantId),
      name: plantName || ("SMA plant " + plantId),
      peak_power_kw: peakKw || null,
      inverter_count: inverters.length,
      energy_today_kwh: Math.round(energyToday * 100) / 100,
      current_power_w: liveW || null,
      error_count_today: inverters.filter((iv) => iv.status === "fault").length,
      status: liveW > 0 ? "producing" : "idle",
      inverters,
    };

    return { provider: "sma", capturedAt: new Date().toISOString(), sites: [site] };
  }

  function hasIntent() {
    return new Promise((res) => {
      try {
        chrome.storage.local.get(INTENT_KEY, (s) => {
          const it = s && s[INTENT_KEY];
          res(!!(it && it.vendor === "sma" && (Date.now() - (it.ts || 0)) < INTENT_TTL_MS));
        });
      } catch (_) { res(false); }
    });
  }
  function clearIntent() { try { chrome.storage.local.remove(INTENT_KEY); } catch (_) {} }

  async function tick() {
    if (done) return;
    polls++;
    if (!(await hasIntent())) return;
    if (!(await isSignedIn())) { broadcastLoginState("login_required"); return; }
    broadcastLoginState("signed_in");
    let payload;
    try { payload = await captureFlow(); } catch (_) { return; }   // retry next tick
    const site = (payload.sites || [])[0];
    if (!site || !(site.inverters || []).length) return;
    const sig = site.site_id + "|" + site.inverters.map((i) => i.serial + ":" + i.energy_today_kwh).join(",");
    const h = await hashString(sig);
    if (h === lastHash) return;
    lastHash = h;
    done = true;
    clearIntent();
    chrome.runtime.sendMessage({ type: "SMA_CAPTURED", payload }, () => void chrome.runtime.lastError);
  }

  tick();
  const iv = setInterval(() => {
    if (done || polls >= MAX_POLLS) { clearInterval(iv); return; }
    tick();
  }, POLL_INTERVAL_MS);
})();
