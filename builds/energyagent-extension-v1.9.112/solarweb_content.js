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
//   /ActualData/GetActualValues?withOnlineState=False&_=<ts>
//     -> [ { PvSystemId, TotalPower (WATTS), DalosOnline:[..], DalosOffline:[..] } ]
//   /Messages/GetUnreadMessageCountForUser?_=<ts>   (200 when signed in)

(function () {
  "use strict";
  var _SO_BROWSER = (typeof window !== "undefined" && typeof location !== "undefined");
  if (_SO_BROWSER && !/(^|\.)solarweb\.com$/.test(location.hostname)) return;

  const INTENT_KEY = "so_capture_intent";   // {vendor, ts} set by background on SO_OPEN_PORTAL
  const SYNC_INTENT_KEY = "so_sync_intent";  // {vendor: ts} per-vendor map armed by a PARALLEL Sync-all
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

  // ── SITE-LOCATION deep-scan (shared, ADDITIVE + FAIL-SAFE) ───────────────────
  // Coordinate field paths in these portals' payloads are NOT confirmed, so rather
  // than guess one path we recursively walk any JSON and return the first plausible
  // {latitude, longitude} pair (and best-effort address string). Everything here is
  // wrapped so a bad shape yields null, never a throw. See each vendor's attach site.
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
  // Join street/city/state/zip/country-ish fields under an address/location object
  // into one string the backend can geocode as a fallback when no coords are found.
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
  // Breadth-first walk. Preferred keys (location/address/site/coordinates/center…)
  // are enqueued first so a top-level or clearly-geographic pair wins over a stray
  // pair buried elsewhere. Returns {latitude, longitude, address?} or null.
  function findLocation(root) {
    try {
      if (!root || typeof root !== "object") return null;
      const queue = [root];
      let seen = 0;
      let addrFallback = null;
      while (queue.length && seen < 4000) {
        const node = queue.shift(); seen++;
        if (!node || typeof node !== "object") continue;

        // A bare [x,y] pair is GeoJSON coordinates → [lng, lat] by spec. Prefer that
        // reading; only fall back to [lat, lng] if the GeoJSON order is out of range
        // (e.g. |first| > 90 means it can't be a latitude, so it must already be lng).
        if (Array.isArray(node) && node.length === 2 &&
            _soCoerceNum(node[0]) != null && _soCoerceNum(node[1]) != null) {
          const asLngLat = _soValidLatLng(node[1], node[0]);   // [lng,lat] (GeoJSON, preferred)
          const asLatLng = _soValidLatLng(node[0], node[1]);   // [lat,lng]
          if (asLngLat) return asLngLat;
          if (asLatLng) return asLatLng;
        }

        if (!Array.isArray(node)) {
          // Direct sibling lat/lng on this object.
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

        // Enqueue children, preferred geographic keys first (breadth-first bias).
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
  // Merge a found location onto a site object IN PLACE (only sets fields present).
  function applyLocation(site, loc) {
    if (!site || !loc) return;
    if (typeof loc.latitude === "number" && typeof loc.longitude === "number") {
      site.latitude = loc.latitude; site.longitude = loc.longitude;
    }
    if (typeof loc.address === "string" && loc.address && site.address == null) site.address = loc.address;
  }

  function LOG() {
    // Surface capture diagnostics in the page console (prefixed for grep-ability).
    try { console.warn.apply(console, ["[solar-operator/fronius]"].concat([].slice.call(arguments))); } catch (_) {}
  }
  if (_SO_BROWSER) LOG("content script loaded v1.9.45 on", location.hostname, "\u2014 watching for a Connect-Fronius capture. If you DON'T see this line, the extension isn't injected on this tab (reload solarweb.com with the extension enabled).");
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

  // Parse Fronius model string -> nameplate kW. "Primo 12.5-1 208-240" -> 12.5,
  // "Symo 20.0-3-M" -> 20.0. The first decimal after the family name is the kW.
  function nameplateFromModel(displayName) {
    if (typeof displayName !== "string") return null;
    const m = displayName.match(/\b(\d+(?:\.\d+)?)\b/);  // first number = kW rating
    return m ? Math.round(parseFloat(m[1]) * 10) / 10 : null;
  }

  // Trapezoidal integral of a [ts_ms, kW] power series -> kWh for the day.
  // Skips None gaps and any interval > 1h (data gap guard).
  function integrateKwh(data) {
    const pts = (data || [])
      .filter((p) => Array.isArray(p) && p.length === 2 && typeof p[1] === "number")
      .sort((a, b) => a[0] - b[0]);
    let kwh = 0;
    for (let i = 1; i < pts.length; i++) {
      const dtH = (pts[i][0] - pts[i - 1][0]) / 3600000;
      if (dtH <= 0 || dtH > 1) continue;
      kwh += ((pts[i - 1][1] + pts[i][1]) / 2) * dtH;
    }
    return Math.round(kwh * 100) / 100;
  }

  // Per-inverter capture for ONE system via the analysis-chart endpoint.
  // Step A: GET the device list (deviceChannels.devices[] = each inverter).
  // Step B: GET the per-device "devwork" (Total Power) series and integrate
  // each inverter's curve to a daily kWh. Returns [] on any failure (the
  // system-level capture still stands).
  async function captureInverters(pvSystemId) {
    const now = new Date();
    const dateQs = "year=" + now.getFullYear() + "&month=" + (now.getMonth() + 1) +
      "&day=" + now.getDate() + "&interval=day";
    let meta;
    try {
      meta = await getJson("/Chart/GetAnalysisChart?pvSystemId=" + encodeURIComponent(pvSystemId) +
        "&" + dateQs + "&compareView=false&kwhkwpView=false&_=" + Date.now());
    } catch (_) { return []; }
    const devices = (((meta || {}).deviceChannels || {}).devices) || [];
    const invDevices = devices.filter((d) => d && d.isActiveDevice !== false &&
      !d.isMeterOrConsumerDevice && d.deviceId);
    if (!invDevices.length) return [];

    const ids = invDevices.map((d) => d.deviceId);
    let dataResp;
    try {
      const devQs = ids.map((id) => "devices=" + encodeURIComponent(id)).join("&");
      dataResp = await getJson("/Chart/GetAnalysisChart?pvSystemId=" + encodeURIComponent(pvSystemId) +
        "&" + dateQs + "&channels=devwork&" + devQs + "&compareView=false&kwhkwpView=false&_=" + Date.now());
    } catch (_) { return []; }

    // Map each "Total Power | <displayName>" series back to its device by name.
    const series = (((dataResp || {}).settings || {}).series) || [];
    const kwhByName = {};
    const peakByName = {};
    const lastByName = {};   // displayName -> { kw, ts_ms } = most-recent power point today
    for (const s of series) {
      const nm = String(s.name || "");
      if (!/Total Power\s*\|/.test(nm)) continue;       // skip the PV-production total series
      const disp = nm.replace(/^.*\|\s*/, "").trim();
      // The devwork series is in WATTS (verified 2026-06-19 by a live daylight
      // capture: a Primo 12.5kW inverter read ~1699, i.e. 1699 W = 1.7 kW, which
      // would be an impossible 1699 kW if it were already kW). Normalize the whole
      // series to kW ONCE here so integrateKwh (→kWh), peak_power_kw, and the live
      // point are all consistently in kW.
      const kwData = (s.data || []).map((p) =>
        (Array.isArray(p) && p.length === 2 && typeof p[1] === "number")
          ? [p[0], p[1] / 1000] : p);
      kwhByName[disp] = integrateKwh(kwData);
      const valid = kwData.filter((p) => Array.isArray(p) && p.length === 2 && typeof p[1] === "number");
      const ys = valid.map((p) => p[1]);
      peakByName[disp] = ys.length ? Math.max(...ys) : null;
      // Last chronological point = this inverter's latest power reading today (kW,
      // after the watts→kW normalization above). Keep its timestamp so we only
      // treat it as LIVE power when recent (see the freshness guard below).
      const sorted = valid.slice().sort((a, b) => a[0] - b[0]);
      const lastPt = sorted.length ? sorted[sorted.length - 1] : null;
      lastByName[disp] = lastPt ? { kw: lastPt[1], ts_ms: lastPt[0] } : null;
    }

    // A devwork point older than this is NOT "current power" — at night the
    // last point is the final daylight reading (hours stale) or zero. Only a
    // recent point is a genuine live reading; otherwise leave current_power_w
    // null so the card honestly shows "produced today · no live feed" (handled
    // in array-operator sandbox.js) instead of a stale value stamped as fresh.
    // Solar.web's devwork chart updates on a coarse cadence (~30 min between
    // points — a live capture 2026-06-19 read points 34–35 min old at midday).
    // A 30-min window would reject those legitimately-recent readings, so use
    // 60 min: recent enough to be "current," loose enough for the source's real
    // granularity. Older than this = a stale final-daylight point, left null.
    const LIVE_FRESH_MS = 60 * 60 * 1000;   // 60 min
    const nowMs = Date.now();

    // DIAGNOSTIC (daylight verification): print each device's raw last point so a
    // single live capture confirms units (kW vs W) + freshness without a rebuild.
    try {
      LOG("per-inverter last devwork point (kW, age_min):",
        invDevices.map((d) => {
          const disp = String(d.displayName || "");
          const lp = lastByName[disp];
          return disp + "=" + (lp ? (lp.kw + "kW/" + Math.round((nowMs - lp.ts_ms) / 60000) + "m") : "none");
        }).join("  "));
    } catch (_) {}

    return invDevices.map((d, i) => {
      const disp = String(d.displayName || ("Inverter " + (i + 1)));
      const peak = peakByName[disp] != null ? peakByName[disp] : null;
      const lp = lastByName[disp];
      // Per-inverter LIVE power (W): the latest devwork point, ONLY if recent.
      const liveW = (lp && lp.kw != null && (nowMs - lp.ts_ms) <= LIVE_FRESH_MS)
        ? Math.round(lp.kw * 1000) : null;
      return {
        serial: String(d.deviceId),               // stable Fronius device GUID = our serial
        name: disp,
        model: disp,
        nameplate_kw: nameplateFromModel(disp),
        energy_today_kwh: kwhByName[disp] != null ? kwhByName[disp] : null,
        peak_power_kw: peak,
        current_power_w: liveW,                    // real per-inverter AC power when fresh, else null
        // No per-inverter fault code in this channel; system-level errors are
        // carried on the site. A zero-energy inverter while peers produce will
        // be flagged "dead" by the peer engine downstream.
      };
    });
  }

  // Daily-kWh HISTORY for instant graph backfill on connect. Reuses the SAME
  // proven /Chart/GetAnalysisChart endpoint (only the date varies) for the last
  // `days` days, integrating every device's "Total Power" curve to daily kWh.
  // Returns BOTH: site[] (summed daily, for the array graph) and byDevice
  // (displayName -> [{date,kwh}], for each inverter's SPARKLINE). Best-effort:
  // any day that fails is skipped, never throws.
  async function captureHistory(pvSystemId, deviceIds, days) {
    const site = [];
    const byDevice = {};                               // displayName -> [{date,kwh}]
    if (!deviceIds || !deviceIds.length) return { site, byDevice };
    const devQs = deviceIds.map((id) => "devices=" + encodeURIComponent(id)).join("&");
    // Build one request per past day and fire them ALL IN PARALLEL. GetAnalysisChart runs ~1.7s
    // each, so the old sequential 1..7 loop was ~12.5s PER SYSTEM — essentially the whole Fronius
    // capture lag. Confirmed live 2026-06-30: 7 parallel = ~3.1s, all 7 succeed (Solar.web doesn't
    // throttle). Promise.all preserves order, so the day series stays chronological. Per-day
    // failures still degrade gracefully to a skipped day (never throws).
    const reqs = [];
    for (let back = 1; back <= days; back++) {          // back=0 (today) already recorded via energy_today_kwh
      const d = new Date();
      d.setDate(d.getDate() - back);
      const dateQs = "year=" + d.getFullYear() + "&month=" + (d.getMonth() + 1) +
        "&day=" + d.getDate() + "&interval=day";
      const iso = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
        "-" + String(d.getDate()).padStart(2, "0");
      reqs.push({ iso, p: getJson("/Chart/GetAnalysisChart?pvSystemId=" + encodeURIComponent(pvSystemId) +
        "&" + dateQs + "&channels=devwork&" + devQs + "&compareView=false&kwhkwpView=false&_=" + Date.now())
        .catch((e) => { LOG("history day fetch failed (skipped):", iso, e && e.message); return null; }) });
    }
    const resps = await Promise.all(reqs.map((r) => r.p));
    for (let i = 0; i < reqs.length; i++) {
      const resp = resps[i];
      if (!resp) continue;
      const iso = reqs[i].iso;
      const series = (((resp || {}).settings || {}).series) || [];
      let dayKwh = 0, any = false;
      for (const s of series) {
        const nm = String(s.name || "");
        if (!/Total Power\s*\|/.test(nm)) continue;     // per-device only
        const disp = nm.replace(/^.*\|\s*/, "").trim();
        const kwh = integrateKwh(s.data);
        dayKwh += kwh; any = true;
        (byDevice[disp] = byDevice[disp] || []).push({ date: iso, kwh: Math.round(kwh * 100) / 100 });
      }
      if (any) site.push({ date: iso, kwh: Math.round(dayKwh * 100) / 100 });
    }
    LOG("history backfill:", pvSystemId, site.length, "site-day(s),", Object.keys(byDevice).length, "device(s)");
    return { site, byDevice };
  }

  // Per-inverter LIVE power from the SAME endpoint Solar.web's REALTIME tab uses
  // (GetActualPvSystemData) — the authoritative instantaneous AC power per device.
  // The analysis 'devwork' chart used by captureInverters lags 30-60 min and isn't a
  // live feed (it left Waterford's cards stuck on a stale near-zero ~4W). Response:
  //   { series:[{ data:[{ name:"Primo 12.5-1 208-240 (1)", custom:{power:12.588,unit:"kW"} }] }],
  //     SensorData:[{ FormatedDateTimeStamp:"06/24/2026 03:34 PM" }] }
  // Returns { byName: {displayName: watts}, ts: ISO, count }. Keyed by display name —
  // which equals the inverter name captureInverters already derives from devwork.
  async function getRealtimePerInverter(pvSystemId) {
    try {
      const r = await getJson("/ActualData/GetActualPvSystemData?pvSystemId=" +
        encodeURIComponent(pvSystemId) + "&_=" + Date.now());
      const data = (((r || {}).series || [])[0] || {}).data || [];
      const byName = {};
      for (const d of data) {
        const nm = String((d && d.name) || "").trim();
        const c = (d && d.custom) || {};
        const p = (typeof c.power === "number") ? c.power : null;
        if (!nm || p == null || p < 0) continue;
        // Solar.web AUTO-SCALES the realtime unit (W / kW / MW by magnitude), so we MUST
        // honor custom.unit. Blindly assuming kW inflated a watts reading 1000x — a 12.5 kW
        // Primo showed "2575 kW" (it was 2575 W = 2.575 kW). Convert to watts by the unit.
        const u = String(c.unit || "kW").trim().toLowerCase();
        const mult = u === "w" ? 1 : u === "mw" ? 1e6 : u === "gw" ? 1e9 : 1000;   // default kW
        byName[nm] = Math.round(p * mult);
      }
      let ts = null;
      try {
        const stamp = (((r || {}).SensorData || [])[0] || {}).FormatedDateTimeStamp;
        const dt = stamp ? new Date(stamp) : null;
        if (dt && !isNaN(dt.getTime())) {
          // Clamp a future-dated parse (browser TZ ≠ system TZ skew) to now — a live
          // reading is never from the future, and the data IS current at capture.
          ts = dt.getTime() > Date.now() ? new Date().toISOString() : dt.toISOString();
        }
      } catch (_) {}
      if (!ts) ts = new Date().toISOString();
      return { byName, ts, count: Object.keys(byName).length };
    } catch (e) {
      LOG("GetActualPvSystemData realtime per-inverter fetch failed:", e && e.message ? e.message : e);
      return { byName: {}, ts: null, count: 0 };
    }
  }

  // Best-effort per-system LOCATION. (a) The list-view row itself is deep-scanned
  // first (free — no extra fetch). (b) If that yields no coordinates, fetch the
  // per-system detail JSON and scan it. Solar.web's JSON API exposes the system at
  // /PvSystems/{id} (same first-party origin the other captures use, session-cookie
  // authed). URL/field paths are INFERRED — NEEDS a live-portal verify. try/catch →
  // any failure returns null and the existing capture is untouched.
  async function captureLocation(system) {
    try {
      const fromRow = findLocation(system);
      if (fromRow && typeof fromRow.latitude === "number") {
        LOG("location FOUND on list row for", system && system.PvSystemName, ":", fromRow);
        return fromRow;
      }
      let rowAddr = fromRow && fromRow.address ? fromRow : null;
      const id = system && system.PvSystemId;
      let detail = null;
      if (id) {
        try {
          detail = await getJson("/PvSystems/" + encodeURIComponent(id) + "?_=" + Date.now());
          const loc = findLocation(detail);
          if (loc && (typeof loc.latitude === "number" || (!rowAddr && loc.address))) {
            LOG("location FOUND on detail endpoint for", system && system.PvSystemName, ":", loc);
            return loc;
          }
        } catch (e) {
          LOG("location detail fetch FAILED for", system && system.PvSystemName, ":",
              e && e.message ? e.message : e);
        }
      }
      if (!rowAddr) {
        // Nothing found anywhere. Dump the top-level keys of both payloads so a
        // console paste tells us EXACTLY what Solar.web's real JSON shape is,
        // without needing live-portal access to design the next fix — see
        // findLocation()'s "endpoint URLs + coord field paths are INFERRED"
        // caveat (ext v1.9.103, commit 001df34).
        LOG("location NOT FOUND for", system && system.PvSystemName,
            "— list-row keys:", system && Object.keys(system),
            "| detail keys:", detail && Object.keys(detail));
      }
      return rowAddr;
    } catch (_) { return null; }
  }

  async function captureFlow() {
    // 1. System list — names, inverter counts, today's energy, specific yield.
    const listResp = await getJson("/PvSystems/GetPvSystemsForListView?_=" + Date.now());
    const systems = (listResp && listResp.data) || [];
    if (!systems.length) throw new Error("no pv systems");

    // 2. Live values — current AC power (WATTS) + online state, keyed by PvSystemId.
    //    Use withOnlineState=False (the proven-working variant from live HAR capture);
    //    online state is best-effort. NEVER silently swallow a live-call failure —
    //    log it loudly so a blank production bar is traceable, but keep capture going
    //    (energy from the list still lands even when live power is unavailable).
    let liveArr = [];
    try {
      liveArr = await getJson("/ActualData/GetActualValues?withOnlineState=False&_=" + Date.now());
      if (!Array.isArray(liveArr)) {
        LOG("GetActualValues returned non-array; live power unavailable:", liveArr);
        liveArr = [];
      }
    } catch (e) {
      LOG("GetActualValues live-power fetch failed; production bar will be blank:", e && e.message ? e.message : e);
      liveArr = [];
    }
    const liveMap = {};
    for (const lv of liveArr) {
      if (lv && lv.PvSystemId) {
        liveMap[lv.PvSystemId] = {
          power_w: typeof lv.TotalPower === "number" ? lv.TotalPower : null,
          online: Array.isArray(lv.DalosOnline) ? lv.DalosOnline.length > 0 : null,
        };
      }
    }

    // 3. Per-system + per-INVERTER drill-down (the sandbox comb). Each system's
    // analysis chart yields every inverter's daily kWh. Best-effort per system.
    // Per-system drill-down. The 7-day history inside each system is now fetched in PARALLEL
    // (see captureHistory) — that was the dominant cost and cut Fronius from ~26s to ~10s.
    // Systems stay SEQUENTIAL on purpose: firing BOTH systems' history at once pushed concurrency
    // to ~16 requests and Solar.web started dropping ~10% (verified live 2026-06-30: 16-parallel =
    // 2 errors vs 7-parallel = 0). History is best-effort, but not worth the loss to shave ~3s.
    const sites = [];
    for (const s of systems) {
      const live = liveMap[s.PvSystemId] || {};
      const energyToday = typeof s.EnergyTodayInkWh === "number" ? s.EnergyTodayInkWh : null;
      let inverters = [];
      try { inverters = await captureInverters(s.PvSystemId); } catch (_) { inverters = []; }
      // Override per-inverter current power with the REALTIME feed (authoritative live
      // AC power) — the devwork point captureInverters used lags and was leaving cards
      // stuck OFFLINE on a stale near-zero. Match by display name; carry the realtime
      // timestamp so the backend's source-freshness signal is honest.
      try {
        const rt = await getRealtimePerInverter(s.PvSystemId);
        if (rt.count) {
          let applied = 0;
          for (const iv of inverters) {
            const w = rt.byName[iv.name];
            if (w != null) { iv.current_power_w = w; iv.last_report = rt.ts; applied++; }
          }
          LOG("realtime per-inverter power applied:", applied, "of", inverters.length, "@", rt.ts);
        }
      } catch (_) {}
      // History backfill: the inverters' serials ARE the Fronius deviceIds, so
      // reuse them to pull the last ~7 days from the same chart endpoint. Returns
      // BOTH site-level totals (array graph) and per-device history (sparklines).
      // Attach each device's daily[] to its inverter by matching displayName.
      let daily = [];
      try {
        const devIds = inverters.map((iv) => iv.serial).filter(Boolean);
        const hist = await captureHistory(s.PvSystemId, devIds, 7);
        daily = hist.site;
        for (const iv of inverters) {
          const dh = hist.byDevice[iv.name] || hist.byDevice[iv.model];
          if (dh && dh.length) iv.daily = dh;          // per-inverter sparkline history
        }
      } catch (_) { daily = []; }
      // Site-level nameplate is DERIVED from today's energy ÷ specific yield —
      // a best-effort estimate, NOT a measured spec rating. Flag it so the
      // backend/dashboard/billing never treat it as an authoritative nameplate.
      const derivedPeakKw = deriveNameplateKw(energyToday, s.KwhPerKwp);
      // Best-effort site LOCATION (coords or geocodable address) so the backend can
      // run the weather model. Purely additive — never blocks the capture.
      let loc = null;
      try { loc = await captureLocation(s); } catch (_) { loc = null; }
      const site = {
        site_id: s.PvSystemId,
        name: s.PvSystemName || null,
        peak_power_kw: derivedPeakKw,
        // true when peak_power_kw came from the energy÷yield derivation above
        // (its only source here); downstream marks it "~est." not measured.
        peak_power_kw_estimated: derivedPeakKw != null,
        inverter_count: typeof s.InverterCount === "number" ? s.InverterCount : null,
        energy_today_kwh: energyToday,
        kwh_per_kwp: typeof s.KwhPerKwp === "number" ? s.KwhPerKwp : null,
        current_power_w: live.power_w != null ? live.power_w : null,
        error_count_today: typeof s.ErrorCntToday === "number" ? s.ErrorCntToday : 0,
        online: live.online,
        status: deriveStatus(live.power_w, s.ErrorCntToday, live.online),
        last_report: parseAspNetDate(s.LastImport) || null,
        last_report_disp: s.LastImportDisp || null,
        daily,                                      // ~7 days site history for instant graph
        inverters,                                  // [] if drill-down unavailable
      };
      applyLocation(site, loc);                     // additive latitude/longitude/address (best-effort)
      sites.push(site);
    }

    return {
      provider: "fronius",
      capturedAt: new Date().toISOString(),
      sites,
    };
  }

  function hasIntent() {
    return new Promise((res) => {
      try {
        chrome.storage.local.get([INTENT_KEY, SYNC_INTENT_KEY], (s) => {
          const it = s && s[INTENT_KEY];
          const single = !!(it && it.vendor === "fronius" && (Date.now() - (it.ts || 0)) < INTENT_TTL_MS);
          const sy = s && s[SYNC_INTENT_KEY];
          const syTs = sy && sy.fronius;
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
        if (!sy || sy.fronius == null) return;   // clear ONLY our vendor so parallel siblings survive
        delete sy.fronius;
        try { chrome.storage.local.set({ [SYNC_INTENT_KEY]: sy }); } catch (_) {}
      });
    } catch (_) {}
  }

  async function tick() {
    if (done) return;
    polls++;
    const intent = await hasIntent();
    LOG("tick #" + polls + " — intent:", intent ? "yes" : "NO (click \u201cConnect Fronius\u201d in Array Operator within 10 min, then return here)");
    if (!intent) return;            // no explicit AO click → never capture
    const signedIn = await isSignedIn();
    LOG("signed in to Solar.web:", signedIn ? "yes" : "NO (sign in at solarweb.com, then it retries automatically)");
    if (!signedIn) { broadcastLoginState("login_required"); return; }
    broadcastLoginState("signed_in");
    let payload;
    try {
      payload = await captureFlow();
    } catch (e) {
      LOG("capture failed this tick (will retry):", (e && e.message) || e);
      return;   // retry on next tick
    }
    LOG("captured systems:", (payload.sites || []).length,
        "— inverters:", (payload.sites || []).reduce((n, s) => n + ((s.inverters || []).length), 0));
    if (!(payload.sites || []).length) return;   // nothing usable yet
    // De-dupe identical captures (e.g. the poller firing twice) by hashing the
    // site ids + their today-energy snapshot.
    const sig = (payload.sites || []).map((s) => s.site_id + ":" + s.energy_today_kwh).join("|");
    const h = await hashString(sig);
    if (h === lastHash) return;
    lastHash = h;
    done = true;
    clearIntent();
    LOG("\u2713 capture complete — shipping to Array Operator. (The 'per-inverter last devwork point' line above is what your dev needs.)");
    chrome.runtime.sendMessage({ type: "FRONIUS_CAPTURED", payload }, () => void chrome.runtime.lastError);
  }

  // TEST HOOK (browser-inert): export PURE helpers for the Node harness. In a
  // browser module is undefined so this is a no-op; the runtime below is unchanged.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      _soCoerceNum, _soValidLatLng, _soExtractAddress, findLocation, applyLocation,
      parseAspNetDate, deriveNameplateKw, deriveStatus, nameplateFromModel, integrateKwh,
    };
  }

  if (_SO_BROWSER) {
    tick();
    const iv = setInterval(() => {
      if (done || polls >= MAX_POLLS) { clearInterval(iv); return; }
      tick();
    }, POLL_INTERVAL_MS);
  }
})();
