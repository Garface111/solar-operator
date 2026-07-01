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
  const SYNC_INTENT_KEY = "so_sync_intent";  // {vendor: ts} per-vendor map armed by a PARALLEL Sync-all
  const INTENT_TTL_MS = 10 * 60 * 1000;
  const POLL_INTERVAL_MS = 1500;
  const MAX_POLLS = 30;
  // Diagnostic trace — flip SMA_DEBUG to true to re-enable the [EnergyAgent SMA]
  // console play-by-play (kept from the v1.9.x debugging saga; silent in prod).
  const SMA_DEBUG = false;
  const LOG = (...a) => { if (!SMA_DEBUG) return; try { console.log("[EnergyAgent SMA]", ...a); } catch (_) {} };
  let polls = 0;
  let lastHash = null;
  let lastLoginState = null;
  let done = false;

  async function hashString(s) {
    const buf = new TextEncoder().encode(String(s));
    const d = await crypto.subtle.digest("SHA-1", buf);
    return Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // ── SITE-LOCATION deep-scan (shared, ADDITIVE + FAIL-SAFE) ───────────────────
  // Coordinate field paths in these portals' payloads are NOT confirmed, so rather
  // than guess one path we recursively walk any JSON and return the first plausible
  // {latitude, longitude} pair (and best-effort address string). Everything here is
  // wrapped so a bad shape yields null, never a throw.
  function _soCoerceNum(v) {
    if (typeof v === "number") return isFinite(v) ? v : null;
    if (typeof v === "string" && v.trim() !== "") { const n = Number(v.trim()); return isFinite(n) ? n : null; }
    return null;
  }
  function _soValidLatLng(lat, lng) {
    lat = _soCoerceNum(lat); lng = _soCoerceNum(lng);
    if (lat == null || lng == null) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    if (lat === 0 && lng === 0) return null;                 // null-island = missing data
    return { latitude: lat, longitude: lng };
  }
  const _SO_LAT_RE = /^(lat|latitude|gpslat|sitelat|centerlat)$/i;
  const _SO_LNG_RE = /^(lng|lon|long|longitude|gpslng|gpslon|sitelng|centerlng)$/i;
  const _SO_PREFER_RE = /^(location|address|site|coordinates|coord|coords|center|geo|position|gps)$/i;
  const _SO_ADDR_KEY_RE = /^(street|street1|address1|addressline1|line1|road|city|town|locality|state|province|region|zip|zipcode|postal|postalcode|postcode|country|countrycode)$/i;
  const _SO_ADDR_FULL_RE = /^(address|addr|fulladdress|formattedaddress|streetaddress|displayaddress)$/i;
  function _soExtractAddress(obj) {
    if (!obj || typeof obj !== "object") return null;
    const parts = [];
    for (const k of Object.keys(obj)) {
      const v = obj[k];
      if (typeof v === "string" && v.trim()) {
        if (_SO_ADDR_FULL_RE.test(k) && v.trim().length > 4) return v.trim();
        if (_SO_ADDR_KEY_RE.test(k)) parts.push(v.trim());
      }
    }
    const joined = parts.join(", ").trim();
    return joined.length > 3 ? joined : null;
  }
  function findLocation(root) {
    try {
      if (!root || typeof root !== "object") return null;
      const queue = [root];
      let seen = 0;
      let addrFallback = null;
      while (queue.length && seen < 4000) {
        const node = queue.shift(); seen++;
        if (!node || typeof node !== "object") continue;
        // A bare [x,y] pair is GeoJSON coordinates → [lng, lat] by spec. Prefer that.
        if (Array.isArray(node) && node.length === 2 &&
            _soCoerceNum(node[0]) != null && _soCoerceNum(node[1]) != null) {
          const asLngLat = _soValidLatLng(node[1], node[0]);   // [lng,lat] (GeoJSON, preferred)
          const asLatLng = _soValidLatLng(node[0], node[1]);   // [lat,lng]
          if (asLngLat) return asLngLat;
          if (asLatLng) return asLatLng;
        }
        if (!Array.isArray(node)) {
          let latKey = null, lngKey = null;
          for (const k of Object.keys(node)) {
            if (latKey == null && _SO_LAT_RE.test(k) && _soCoerceNum(node[k]) != null) latKey = k;
            else if (lngKey == null && _SO_LNG_RE.test(k) && _soCoerceNum(node[k]) != null) lngKey = k;
          }
          if (latKey != null && lngKey != null) {
            const hit = _soValidLatLng(node[latKey], node[lngKey]);
            if (hit) {
              const addr = _soExtractAddress(node);
              return addr ? Object.assign(hit, { address: addr }) : hit;
            }
          }
          if (!addrFallback) { const a = _soExtractAddress(node); if (a) addrFallback = a; }
        }
        const kids = Array.isArray(node) ? node.map((_, i) => i) : Object.keys(node);
        const preferred = [], rest = [];
        for (const k of kids) {
          const child = node[k];
          if (child && typeof child === "object") {
            (!Array.isArray(node) && _SO_PREFER_RE.test(String(k)) ? preferred : rest).push(child);
          }
        }
        for (const c of preferred) queue.push(c);
        for (const c of rest) queue.push(c);
      }
      return addrFallback ? { address: addrFallback } : null;
    } catch (_) { return null; }
  }
  function applyLocation(site, loc) {
    if (!site || !loc) return;
    if (typeof loc.latitude === "number" && typeof loc.longitude === "number") {
      site.latitude = loc.latitude; site.longitude = loc.longitude;
    }
    if (typeof loc.address === "string" && loc.address && site.address == null) site.address = loc.address;
  }

  // ennexOS authenticates to uiapi.sunnyportal.com with a Keycloak OAuth Bearer
  // token (NOT a cookie). The token lives in the page's localStorage under
  // "access_token" — content scripts share localStorage with the host page, so
  // we read it directly and send it as `Authorization: Bearer …`, exactly like
  // SMA's own SPA. Crucially we do NOT use credentials:"include" — that combined
  // with the API's Access-Control-Allow-Origin:* is what the browser CORS-blocks.
  // No-credentials + Bearer header matches the SPA and passes CORS cleanly.
  function getAccessToken() {
    try { return localStorage.getItem("access_token"); } catch (_) { return null; }
  }
  async function getJson(url) {
    const tok = getAccessToken();
    if (!tok) throw new Error("no access_token in localStorage (not logged in?)");
    let r;
    try {
      r = await fetch(url, {
        headers: { "Authorization": "Bearer " + tok, "Accept": "application/json" },
        // default credentials mode ("same-origin") → no cookies cross-origin → CORS ok with ACAO:*
      });
    } catch (e) {
      try { if (SMA_DEBUG) console.log("[EnergyAgent SMA] GET", url, "-> NETWORK/CORS FAIL", e && e.message); } catch (_) {}
      throw e;
    }
    try { if (SMA_DEBUG) console.log("[EnergyAgent SMA] GET", url, "->", r.ok ? "ok " + r.status : "FAIL status=" + r.status); } catch (_) {}
    if (!r.ok) throw new Error("api " + r.status);
    return r.json();
  }
  // POST counterpart to getJson — same Bearer auth + no-credentials style (the
  // measurements/search endpoint is a POST with a JSON body). Used for the
  // site-level live-power query.
  async function postJson(url, body) {
    const tok = getAccessToken();
    if (!tok) throw new Error("no access_token in localStorage (not logged in?)");
    let r;
    try {
      r = await fetch(url, {
        method: "POST",
        headers: {
          "Authorization": "Bearer " + tok,
          "Accept": "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
        // default credentials mode ("same-origin") → no cookies cross-origin → CORS ok with ACAO:*
      });
    } catch (e) {
      try { if (SMA_DEBUG) console.log("[EnergyAgent SMA] POST", url, "-> NETWORK/CORS FAIL", e && e.message); } catch (_) {}
      throw e;
    }
    try { if (SMA_DEBUG) console.log("[EnergyAgent SMA] POST", url, "->", r.ok ? "ok " + r.status : "FAIL status=" + r.status); } catch (_) {}
    if (!r.ok) throw new Error("api " + r.status);
    return r.json();
  }
  // navigation/menuitems returns 200 JSON when the Bearer token is valid.
  async function isSignedIn() {
    try {
      await getJson(UIAPI + "/api/v1/navigation/menuitems");
      return true;
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

  // Live whole-site AC power. The /overview/.../devices endpoint DRIFTED — its
  // d.pvPower is now null. Live power moved to POST /api/v1/measurements/search:
  // query channel Measurement.GridMs.TotW.Pv at the PLANT-level component (the
  // plantId itself) gives whole-site AC power in WATTS, 15-min buckets. The LAST
  // entry with a finite numeric value is "now". Returns a number (W) or null.
  let _smaLastSourceTs = null;   // SMA source's last live-data timestamp (gauge ts), per plant
  async function fetchSiteLivePowerW(plantId) {
    _smaLastSourceTs = null;
    // PRIMARY: the portal's own live-power gauge — a single clean watts number.
    // GET /api/v1/widgets/gauge/power?componentId=<plantId>&type=PvProduction
    //   -> {"value":40691,"timestamp":"...","min":0,"max":140000}  (watts)
    // This is exactly what Sunny Portal's live gauge reads; grounded in the HAR.
    try {
      const g = await getJson(UIAPI + "/api/v1/widgets/gauge/power?componentId="
        + encodeURIComponent(String(plantId)) + "&type=PvProduction");
      if (g && typeof g.value === "number" && isFinite(g.value)) {
        // The gauge's timestamp IS the source's last live-data time — keep it so the
        // backend can tell fresh data from a frozen value we keep re-scraping.
        try { const _d = new Date(g.timestamp); if (!isNaN(_d.getTime())) _smaLastSourceTs = _d.toISOString(); } catch (_) {}
        return g.value;
      }
    } catch (e) {
      LOG("gauge/power fetch failed, trying measurements/search:", e && e.message || e);
    }
    // FALLBACK: the 15-min measurement series — take the last finite sample.
    // Day window in UTC: today 04:00Z → tomorrow 04:00Z (covers an EDT/EST day).
    const _now = new Date();
    const begin = new Date(Date.UTC(_now.getUTCFullYear(), _now.getUTCMonth(), _now.getUTCDate(), 4, 0, 0, 0));
    if (_now.getTime() < begin.getTime()) begin.setUTCDate(begin.getUTCDate() - 1);
    const end = new Date(begin.getTime() + 24 * 60 * 60 * 1000);
    const body = {
      queryItems: [{
        componentId: String(plantId),
        channelId: "Measurement.GridMs.TotW.Pv",
        resolution: "FifteenMinutes",
        timezone: "America/New_York",
        aggregate: "Avg",
        multiAggregate: "Sum",
      }],
      dateTimeBegin: begin.toISOString(),
      dateTimeEnd: end.toISOString(),
    };
    const res = await postJson(UIAPI + "/api/v1/measurements/search", body);
    const series = Array.isArray(res)
      ? res.find((s) => s && s.channelId === "Measurement.GridMs.TotW.Pv"
          && String(s.componentId) === String(plantId))
        || res.find((s) => s && Array.isArray(s.values))
      : null;
    const values = (series && Array.isArray(series.values)) ? series.values : [];
    for (let i = values.length - 1; i >= 0; i--) {
      const v = values[i] && values[i].value;
      if (typeof v === "number" && isFinite(v)) return v;
    }
    return null;
  }

  // Discover which plant(s) to capture. ennexOS /navigation is a tree-walker:
  //   /navigation/menuitems            -> Portfolio (componentId null) at root
  //   /navigation/menuitems?componentId=X -> that Plant
  //   /navigation?parentId=X           -> children (devices) of plant X
  //   /navigation        (no param)    -> root children = the owner's Plant(s)
  // Returns an array of {id, name}. Handles: (a) the owner sitting ON a plant
  // (URL has the id), and (b) the owner on the portfolio root (enumerate all).
  async function resolvePlants() {
    const out = [];
    const seen = new Set();
    const add = (id, name) => { id = String(id || ""); if (id && !seen.has(id)) { seen.add(id); out.push({ id, name: name || null }); } };

    // Enumerate the WHOLE portfolio FIRST — a multi-plant owner must get EVERY plant, even when
    // the SPA is deep-linked to just one of them. The old code short-circuited on a plant id in
    // the URL path and returned ONLY that plant, silently dropping the rest (e.g. "SMA: 1 array"
    // for a 2-plant portfolio when the tab happened to open on one system). Confirmed live
    // 2026-06-30: GET /api/v1/navigation returns ALL Plant roots regardless of the current route.
    try {
      const roots = await getJson(UIAPI + "/api/v1/navigation");
      if (Array.isArray(roots)) {
        for (const c of roots) {
          if (c && c.componentType === "Plant" && c.componentId) add(c.componentId, c.name);
        }
      }
    } catch (_) { /* fall through */ }
    if (out.length) return out;

    // Single-plant accounts may resolve straight to their plant via menuitems.
    try {
      const nav = await getJson(UIAPI + "/api/v1/navigation/menuitems");
      if (nav && nav.componentType === "Plant" && nav.componentId) add(nav.componentId, nav.name);
    } catch (_) { /* none */ }
    if (out.length) return out;

    // LAST RESORT only — the plant id in the URL path, when enumeration is unavailable (owner
    // sitting on one plant and both API calls failed). Never the primary path: it can't see the
    // owner's other plants.
    const m = location.pathname.match(/\/(\d{4,})\b/);
    if (m) add(m[1], null);

    return out;   // possibly empty — caller retries next poll (SPA may still load)
  }

  // Daily-kWh HISTORY for any component (plant OR device) for instant graph
  // backfill on connect. SMA exposes historical daily energy via POST
  // /measurements/search with the metering channel at OneDay + Dif (Wh/day).
  // Plant componentId → array graph; device componentId → per-inverter sparkline.
  // Best-effort: any failure/unexpected shape → [] (graph builds up naturally),
  // never fabricate. NOTE: the PLANT query is grounded; the per-DEVICE query
  // reuses the same channel/shape and is best-effort (empty if SMA scopes the
  // metering channel plant-only — harmless, just leaves sparklines to accumulate).
  async function fetchHistory(componentId, days) {
    const _now = new Date();
    const end = new Date(Date.UTC(_now.getUTCFullYear(), _now.getUTCMonth(), _now.getUTCDate(), 4, 0, 0, 0));
    const begin = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
    const body = {
      queryItems: [{
        componentId: String(componentId),
        channelId: "Measurement.Metering.TotWhOut.Pv",
        resolution: "OneDay",
        timezone: "America/New_York",
        aggregate: "Dif",
      }],
      dateTimeBegin: begin.toISOString(),
      dateTimeEnd: end.toISOString(),
    };
    let res;
    try { res = await postJson(UIAPI + "/api/v1/measurements/search", body); }
    catch (e) { LOG("history search failed (skipped):", componentId, e && e.message || e); return []; }
    const series = Array.isArray(res)
      ? (res.find((s) => s && s.channelId === "Measurement.Metering.TotWhOut.Pv")
          || res.find((s) => s && Array.isArray(s.values)))
      : null;
    const values = (series && Array.isArray(series.values)) ? series.values : [];
    const out = [];
    for (const v of values) {
      const wh = v && v.value;
      if (typeof wh !== "number" || !isFinite(wh) || wh < 0) continue;
      const ts = v.time || v.timestamp || v.date;
      if (!ts) continue;
      const d = new Date(ts);
      if (isNaN(d.getTime())) continue;
      const iso = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
        "-" + String(d.getDate()).padStart(2, "0");
      out.push({ date: iso, kwh: Math.round((wh / 1000) * 100) / 100 });
    }
    return out;
  }

  // Best-effort plant LOCATION (coords or geocodable address) for the weather model.
  // GET /api/v1/plants/{plantId} (same uiapi origin + Bearer auth as everything else)
  // is where ennexOS carries plant metadata; deep-scan whatever it returns. The
  // location fields aren't confirmed in a live payload — INFERRED, NEEDS a live-portal
  // verify. try/catch → any failure returns null, capture proceeds unchanged.
  async function fetchPlantLocation(plantId) {
    try {
      const plant = await getJson(UIAPI + "/api/v1/plants/" + encodeURIComponent(plantId));
      return findLocation(plant);
    } catch (_) { return null; }
  }

  // Capture one plant's per-inverter comb. Returns a site object or null.
  async function captureOnePlant(plantId, hintName) {
    let plantName = hintName || null;
    if (!plantName) {
      try {
        const nav = await getJson(UIAPI + "/api/v1/navigation/menuitems?componentId=" + encodeURIComponent(plantId));
        if (nav && nav.name) plantName = nav.name;
      } catch (_) {}
    }
    // The devices endpoint 500s WITHOUT ?todayDate=YYYY-MM-DD (it's a required
    // query param — confirmed against the working portal call). Use the browser's
    // LOCAL date, matching what the SPA sends (the plant's "today").
    const _now = new Date();
    const todayDate = _now.getFullYear() + "-" +
      String(_now.getMonth() + 1).padStart(2, "0") + "-" +
      String(_now.getDate()).padStart(2, "0");
    const devices = await getJson(UIAPI + "/api/v1/overview/" + plantId + "/devices?todayDate=" + todayDate);
    const inverters = (devices || [])
      // Keep every real inverter Device. DO NOT gate on per-device pvPower: the
      // /overview/.../devices endpoint DRIFTED and now serves pvPower=null (see
      // fetchSiteLivePowerW comment), so requiring it dropped EVERY inverter →
      // captureOnePlant returned null → the whole SMA capture failed and the
      // cards froze at the last reading that happened to carry pvPower. Live
      // power now comes from the site-level measurements/gauge call below and is
      // allocated across inverters by the backend (same as Fronius). An inverter
      // qualifies if it reports live power OR today's energy OR just has an id.
      .filter((d) => {
        if (!d || d.componentType !== "Device") return false;
        // EXCLUDE non-inverter devices SMA lists alongside inverters — the Energy
        // Data Manager (model EDMM-10 / names like "…Datamanager"), Sunny Home
        // Manager, energy meters, gateways. They never produce (no pvPower/energy),
        // so the hasId fallback below would otherwise capture them AS inverters
        // (they'd show up as a permanently-0 kW "dead" inverter).
        const tag = `${d.product || ""} ${d.name || ""}`.toLowerCase();
        if (/\bedmm\b|data\s*manager|datamanager|home\s*manager|energy\s*meter|\bmeter\b|webconnect|gateway|cluster\s*controller/.test(tag)) return false;
        const hasPower = typeof d.pvPower === "number";
        const hasEnergy = typeof d.totWhOutToday === "number";
        const hasId = d.serial != null || d.componentId != null;
        return hasPower || hasEnergy || hasId;
      })
      .map((d) => ({
        serial: String(d.serial || d.componentId),
        name: d.name || String(d.serial),
        model: d.product || null,
        nameplate_kw: nameplateKw(d.product, d.name),
        energy_today_kwh: typeof d.totWhOutToday === "number" ? d.totWhOutToday / 1000.0 : null,
        current_power_w: typeof d.pvPower === "number" ? d.pvPower : null,
        status: deriveStatus(d),
        _componentId: d.componentId != null ? String(d.componentId) : null,  // for per-device history (stripped before send)
      }));
    if (!inverters.length) return null;

    let energyToday = 0, sumW = 0, peakKw = 0, haveSumW = false;
    inverters.forEach((iv) => {
      if (iv.energy_today_kwh) energyToday += iv.energy_today_kwh;
      if (typeof iv.current_power_w === "number") { sumW += iv.current_power_w; haveSumW = true; }
      if (iv.nameplate_kw) peakKw += iv.nameplate_kw;
    });

    // Live site AC power now comes from a dedicated measurements/search POST (the
    // devices endpoint's pvPower drifted to null). Never let this break capture —
    // energy_today must still land even if the live query fails.
    let siteW = null;
    try {
      siteW = await fetchSiteLivePowerW(plantId);
    } catch (e) {
      LOG("site live-power fetch failed (energy still captured):", e && e.message || e);
    }
    // Prefer the authoritative site-level measurement; fall back to the
    // per-inverter sum only when devices actually reported numeric power.
    // Use a real null check — a legit 0 W is valid, only null when no data.
    let currentPowerW = null;
    if (typeof siteW === "number") currentPowerW = siteW;
    else if (haveSumW) currentPowerW = sumW;

    // History backfill (best-effort; empty just lets the graph build up).
    // Plant-level → array graph. Per-device → each inverter's sparkline.
    let daily = [];
    try { daily = await fetchHistory(plantId, 7); } catch (_) { daily = []; }
    for (const iv of inverters) {
      if (iv._componentId) {
        try {
          const dh = await fetchHistory(iv._componentId, 7);
          if (dh && dh.length) iv.daily = dh;        // per-inverter sparkline history
        } catch (_) { /* best-effort */ }
      }
      delete iv._componentId;                         // strip temp field before send
    }
    LOG("SMA history:", plantId, daily.length, "site-day(s);",
        inverters.filter((iv) => (iv.daily || []).length).length, "inv w/ history");

    // Best-effort location (never blocks capture).
    let loc = null;
    try { loc = await fetchPlantLocation(plantId); } catch (_) { loc = null; }

    const site = {
      site_id: String(plantId),
      name: plantName || ("SMA plant " + plantId),
      peak_power_kw: peakKw || null,
      inverter_count: inverters.length,
      energy_today_kwh: Math.round(energyToday * 100) / 100,
      current_power_w: currentPowerW,
      error_count_today: inverters.filter((iv) => iv.status === "fault").length,
      status: (typeof currentPowerW === "number" && currentPowerW > 0) ? "producing" : "idle",
      last_report: _smaLastSourceTs,              // source's last live-data time (gauge ts) — honest freshness
      daily,                                      // ~7 days site history for instant graph
      inverters,
    };
    applyLocation(site, loc);                     // additive latitude/longitude/address (best-effort)
    return site;
  }

  async function captureFlow() {
    const plants = await resolvePlants();
    if (!plants.length) throw new Error("no plants found");

    const sites = [];
    for (const p of plants) {
      try {
        const site = await captureOnePlant(p.id, p.name);
        if (site) sites.push(site);
      } catch (_) { /* skip a plant that errored; others still ship */ }
    }
    if (!sites.length) throw new Error("no producing inverters");
    return { provider: "sma", capturedAt: new Date().toISOString(), sites };
  }

  function hasIntent() {
    return new Promise((res) => {
      try {
        chrome.storage.local.get([INTENT_KEY, SYNC_INTENT_KEY], (s) => {
          const it = s && s[INTENT_KEY];
          const single = !!(it && it.vendor === "sma" && (Date.now() - (it.ts || 0)) < INTENT_TTL_MS);
          const sy = s && s[SYNC_INTENT_KEY];
          const syTs = sy && sy.sma;
          const sync = !!(syTs && (Date.now() - syTs) < INTENT_TTL_MS);
          res(single || sync);
        });
      } catch (_) { res(false); }
    });
  }
  function clearIntent() {
    try { chrome.storage.local.remove(INTENT_KEY); } catch (_) {}
    try {
      chrome.storage.local.get(SYNC_INTENT_KEY, (s) => {
        const sy = s && s[SYNC_INTENT_KEY];
        if (!sy || sy.sma == null) return;   // clear ONLY our vendor so parallel siblings survive
        delete sy.sma;
        try { chrome.storage.local.set({ [SYNC_INTENT_KEY]: sy }); } catch (_) {}
      });
    } catch (_) {}
  }

  let lastErr = null;   // most recent failure reason, surfaced to the AO page on give-up
  function reportFailure(reason) {
    chrome.runtime.sendMessage(
      { type: "SMA_CAPTURE_FAILED", reason: String(reason || "unknown"), url: location.href },
      () => void chrome.runtime.lastError,
    );
  }

  // Loud, prefixed console trace so the SMA tab's console shows EXACTLY how far
  // each attempt gets. Search the console for "[EnergyAgent SMA]". (Gated by
  // SMA_DEBUG, declared at top — silent in prod.)
  LOG("content script loaded on", location.href);

  async function tick() {
    if (done) return;
    polls++;
    LOG("tick #" + polls);
    const intent = await hasIntent();
    LOG("hasIntent:", intent);
    if (!intent) { lastErr = "capture not requested from Array Operator (open Add array → Log in with SMA)"; return; }
    let signedIn;
    try { signedIn = await isSignedIn(); }
    catch (e) { lastErr = "auth-check failed: " + (e && e.message || e); LOG("isSignedIn threw:", e); return; }
    LOG("signedIn:", signedIn);
    if (!signedIn) { lastErr = "not signed in to SMA (or the API rejected the session cookie)"; broadcastLoginState("login_required"); return; }
    broadcastLoginState("signed_in");
    let payload;
    try { payload = await captureFlow(); }
    catch (e) { lastErr = "capture failed: " + (e && e.message || e); LOG("captureFlow threw:", e); return; }   // retry next tick
    const sites = payload.sites || [];
    LOG("captureFlow returned sites:", sites.length, sites.map((s) => s.name + "(" + (s.inverters || []).length + ")"));
    if (!sites.length) { lastErr = "signed in, but no plants/inverters returned"; return; }
    const sig = sites.map((s) =>
      s.site_id + "|" + (s.inverters || []).map((i) => i.serial + ":" + i.energy_today_kwh).join(",")
    ).join("||");
    const h = await hashString(sig);
    if (h === lastHash) return;
    lastHash = h;
    done = true;
    clearIntent();
    LOG("CAPTURED — sending to Array Operator:", sites.length, "plant(s)");
    chrome.runtime.sendMessage({ type: "SMA_CAPTURED", payload }, () => void chrome.runtime.lastError);
  }

  // 400 "Request Too Long" recovery (mirrors the Fronius path): if sunnyportal rejects because
  // the accumulated Cookie header outgrew the server limit, the app never loads — clear this
  // vendor's cookies once and reload so the owner lands on a clean login instead of a dead 400.
  // One-shot per tab (sessionStorage guard) so a persistent error can't loop.
  function maybeRecoverFrom400() {
    try {
      const t = (document.title || "") + " " + (document.body ? String(document.body.innerText || "").slice(0, 600) : "");
      if (!/Request Too Long|request headers .{0,14}too long|Error 400/i.test(t)) return false;
      if (sessionStorage.getItem("so_sma_400_recovered") === "1") return false;
      sessionStorage.setItem("so_sma_400_recovered", "1");
      LOG("400 Request-Too-Long detected — clearing bloated SMA cookies + reloading to a clean login");
      chrome.runtime.sendMessage({ type: "SO_RECOVER_VENDOR", vendor: "sma" }, () => {
        try { void chrome.runtime.lastError; } catch (_) {}
        setTimeout(() => { try { location.reload(); } catch (_) {} }, 500);
      });
      return true;
    } catch (_) { return false; }
  }

  if (maybeRecoverFrom400()) return;   // recovering from a 400 — skip the normal capture loop this load
  tick();
  // Fast token-watcher: SMA's Bearer token only lands in localStorage AFTER the heavy
  // ennexOS SPA boots + Keycloak auth completes — often several poll intervals on a fresh
  // background tab. Re-run tick() the instant it appears instead of waiting for the next
  // scheduled poll, so a logged-in SMA capture lands almost as fast as the cookie vendors.
  let _tokenSeen = false;
  const _tokWatch = setInterval(() => {
    if (done || _tokenSeen) { clearInterval(_tokWatch); return; }
    try { if (getAccessToken()) { _tokenSeen = true; clearInterval(_tokWatch); tick(); } } catch (_) {}
  }, 250);
  const iv = setInterval(() => {
    if (done) { clearInterval(iv); return; }
    if (polls >= MAX_POLLS) {
      clearInterval(iv);
      // Out of retries with no capture — tell the AO page WHY instead of leaving
      // its spinner hanging forever.
      reportFailure(lastErr || "timed out with no data");
      return;
    }
    tick();
  }, POLL_INTERVAL_MS);
})();
