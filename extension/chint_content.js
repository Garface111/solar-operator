// chint_content.js — runs on monitor.chintpowersystems.com (Chint Power Systems
// / CPS "Monitor" portal — the real owner-facing monitoring site).
//
// Array Operator PER-INVERTER capture for CHINT / CPS.
//
// ── APPROACH: PASSIVE RESPONSE OBSERVATION (after token-replay failed) ─────────
// HAR-grounding (2026-06-16, Bruce's live "Londonderry 186" account) gave us the
// real endpoints + JSON shapes. BUT Chint's auth is a CryptoJS-encrypted token
// bound per-request — replaying the observed token returned 4010 "Please login"
// even from the page's own context. So we DON'T call the API ourselves.
//
// Instead chint_inject.js (MAIN world) hooks the app's OWN XHR/fetch RESPONSES
// and relays the bodies for two endpoints to us here:
//   /api/asset/site/retrieve        → site list (loads on dashboard)
//   /api/asset/site/busTypeDevices  → a site's inverters (loads when owner opens it)
// We parse those responses, assemble the per-inverter payload, and ship it. Zero
// auth replay → cannot 4010. The tradeoff: the owner must open each site once so
// the app fetches its devices; we capture whatever sites they've visited.
//
// JSON shapes (verified live):
//   retrieve.data[]        : { id, siteName, installedCapacity(kW str), currentPower(W str) }
//   busTypeDevices.data    : { id(siteId), gwDevices:[ { commDevices:[ {
//        assetTypeName:"Inverter", sn, assetAlias, model, currentPower(W str),
//        eToday(kWh num), statusName } ] } ] }
//
// PRIVACY: capture runs ONLY when a recent, explicit AO "Connect Chint" click set
// the intent flag. SAFETY: read-only — we observe the owner's own data, never
// inject requests; nothing persists beyond the in-memory hand-off.

(function () {
  "use strict";
  if (!/(^|\.)chintpowersystems\.com$/.test(location.hostname)) return;

  const CHINT_DEBUG = true;
  const LOG = (...a) => { if (CHINT_DEBUG) { try { console.log("[EnergyAgent CHINT]", ...a); } catch (_) {} } };
  LOG("content script LOADED on", location.href);

  const INTENT_KEY = "so_capture_intent";
  const INTENT_TTL_MS = 10 * 60 * 1000;
  const POLL_INTERVAL_MS = 1500;
  const MAX_POLLS = 400;                       // ~10 min @1.5s (matches INTENT_TTL) — a first-time owner
                                               // needs time to SIGN IN before the capture can run; was
                                               // 100 (~2.5min), which expired while Bruce was logging in
  let polls = 0;
  let lastHash = null;
  let lastLoginState = null;
  let lastErr = null;
  let done = false;
  let _warnedNoList = false;
  let emittedAny = false;
  let _walkStarted = false;   // v1.9.77: have we kicked the auto site-walk yet?
  let _walkDone = false;      // v1.9.79: chint_inject signalled the walk visited every site
  let _walkExpected = 0;      // v1.9.79: how many sites the walk was asked to visit

  // Observed response bodies, relayed by chint_inject.js (MAIN world).
  let siteListJson = null;                     // parsed /api/asset/site/retrieve
  const deviceJsonBySite = new Map();          // siteId -> parsed busTypeDevices
  const dailyBySite = new Map();               // siteId -> [{date,kwh}] (history backfill)
  // serial -> last genuinely-nonzero live watts seen THIS session. Lets us OMIT a
  // transient/partial 0 (a mid-reload blip) so the backend keeps the prior good live
  // value instead of flashing a false "0 kW, live now" inside its 15-min fresh window.
  // A REAL off-state (status offline/fault) keeps its 0 — only unexplained 0s are held.
  const lastGoodPower = new Map();

  function tryParse(body) { try { return JSON.parse(body); } catch (_) { return null; } }

  // ── SITE-LOCATION deep-scan (shared, ADDITIVE + FAIL-SAFE) ───────────────────
  // Coordinate field paths in the Chint payloads are NOT confirmed, so rather than
  // guess one path we recursively walk any JSON and return the first plausible
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

  // Integrate the site's 30-min PV POWER curve into daily kWh. The production
  // chart endpoint (/openApi/v1/siteMertics/getSiteTimeSharingChart2) returns
  // data.times[] ("YYYY-MM-DD HH:MM") + data.pv[] (instantaneous kW per slot).
  // kWh for a day = Σ(pv_kW × interval_hours). interval is in the URL (&interval=30
  // minutes); default to 30 if absent. Grounded on Bruce's Londonderry HAR
  // (2026-06-17): 6/15→1499, 6/16→1671 kWh — plausible for a 186 kW site.
  // Site-level only (Chint exposes no per-inverter history) → never split per inv.
  function dailyFromChart(json, search) {
    const d = json && json.data;
    if (!d || !Array.isArray(d.times)) return [];
    // prefer the dedicated PV series; fall back to the generic "metrics" series.
    const series = Array.isArray(d.pv) && d.pv.length ? d.pv
      : (Array.isArray(d.metrics) ? d.metrics : []);
    if (!series.length) return [];
    let stepMin = 30;
    const m = /[?&]interval=(\d+)/.exec(String(search || ""));
    if (m) { const n = parseInt(m[1], 10); if (isFinite(n) && n > 0) stepMin = n; }
    const stepH = stepMin / 60.0;
    const byDay = new Map();
    for (let i = 0; i < d.times.length; i++) {
      const t = String(d.times[i] || "");
      const day = t.split(" ")[0];                 // "2026-06-15"
      if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
      const kw = Number(series[i]);
      if (!isFinite(kw) || kw <= 0) { if (!byDay.has(day)) byDay.set(day, 0); continue; }
      byDay.set(day, (byDay.get(day) || 0) + kw * stepH);
    }
    const out = [];
    for (const [day, kwh] of byDay) out.push({ date: day, kwh: Math.round(kwh * 10) / 10 });
    return out;
  }

  // Pull siteId out of the chart request's query string.
  function siteIdFromSearch(search) {
    const m = /[?&]siteId=([^&]+)/.exec(String(search || ""));
    return m ? decodeURIComponent(m[1]) : null;
  }

  // The site/retrieve response carries weekETrend[] — the last ~7 days of daily
  // site kWh as [{name:"20260610", value:"996.2"}]. The dashboard fetches it on
  // load (we ALREADY observe site/retrieve), so this is free, passive history —
  // no extra navigation, no auth replay. Map it to [{date:"YYYY-MM-DD", kwh}].
  function weekTrendDaily(st) {
    const wt = st && Array.isArray(st.weekETrend) ? st.weekETrend : [];
    const out = [];
    for (const p of wt) {
      const m = /^(\d{4})(\d{2})(\d{2})$/.exec(String((p && p.name) || ""));
      if (!m) continue;
      const kwh = Number(p && p.value);
      if (!isFinite(kwh) || kwh < 0) continue;
      out.push({ date: m[1] + "-" + m[2] + "-" + m[3], kwh: Math.round(kwh * 10) / 10 });
    }
    return out;
  }
  // Merge daily kWh series by date, max-wins (matches the backend's dedup), ascending.
  function mergeDaily() {
    const m = new Map();
    for (let i = 0; i < arguments.length; i++) {
      for (const d of (arguments[i] || [])) {
        if (!d || !d.date || d.kwh == null) continue;
        const prev = m.get(d.date);
        if (prev == null || d.kwh > prev) m.set(d.date, d.kwh);
      }
    }
    return Array.from(m.keys()).sort().map((k) => ({ date: k, kwh: m.get(k) }));
  }

  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin) return;
    const d = e.data;
    // The walk (chint_inject.js) signals when it has stepped through every site, so a
    // silent recap/sync-all capture knows its snapshot is complete and can close.
    if (d && d.type === "SO_CHINT_WALK_DONE") { _walkDone = true; LOG("walk done — every site visited"); return; }
    if (!d || d.type !== "SO_CHINT_RESPONSE" || !d.path) return;
    const j = tryParse(d.body);
    if (!j || (j.code != null && String(j.code) !== "0")) {
      LOG("observed response (ignored, code", j && j.code, ")", d.path);
      return;
    }
    if (d.path === "/api/asset/site/retrieve") {
      if (Array.isArray(j.data)) { siteListJson = j; LOG("observed SITE LIST:", j.data.length, "site(s)"); }
    } else if (d.path === "/api/asset/site/busTypeDevices") {
      const sid = j.data && j.data.id;
      if (sid) {
        deviceJsonBySite.set(String(sid), j);
        const n = countInverters(j);
        LOG("observed DEVICES for site", sid, "->", n, "inverter(s)");
        // Tell the MAIN-world walk this site's devices actually LANDED, so it advances the
        // moment the data arrives instead of on a blind fixed timer (which sometimes moved on
        // before the response came back → empty capture → stuck).
        try { window.postMessage({ type: "SO_CHINT_SITE_OBSERVED", siteId: String(sid) }, location.origin); } catch (_) {}
      }
    } else if (d.path === "/openApi/v1/siteMertics/getSiteTimeSharingChart2") {
      // Production chart — integrate its 30-min PV power curve into daily kWh
      // for the instant history backfill. siteId comes from the query string.
      const sid = siteIdFromSearch(d.search);
      if (sid) {
        const daily = dailyFromChart(j, d.search);
        if (daily.length) {
          dailyBySite.set(String(sid), daily);
          LOG("observed PRODUCTION CHART for site", sid, "->", daily.length, "day(s) of kWh");
        }
      }
    }
  });

  async function hashString(s) {
    const buf = new TextEncoder().encode(String(s));
    const dd = await crypto.subtle.digest("SHA-1", buf);
    return Array.from(new Uint8Array(dd)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  function num(v) { const n = Number(v); return isFinite(n) ? n : null; }
  function kwFromStr(v) { const n = Number(v); return isFinite(n) ? Math.round(n * 1000) / 1000 : null; }
  // Parse a power string with unit into WATTS, e.g. "72.7 KW" -> 72700.
  // Units (case-insensitive): W=1, KW/kW=1000, MW=1e6. Returns null if unparseable.
  function parsePowerToW(v) {
    if (v == null) return null;
    const m = String(v).trim().match(/^(-?[\d.]+)\s*([kKmM]?[wW])$/);
    if (!m) return null;
    const n = Number(m[1]);
    if (!isFinite(n)) return null;
    const unit = m[2].toLowerCase();
    const mult = unit === "mw" ? 1e6 : unit === "kw" ? 1000 : 1;
    return Math.round(n * mult);
  }
  function mapStatus(statusName, powerW) {
    const s = String(statusName || "").toLowerCase();
    if (/fault|error|alarm|warn/.test(s)) return "fault";
    if (/off|disconnect|standby|stop/.test(s)) return "offline";
    return (powerW || 0) > 0 ? "producing" : "idle";
  }
  function invertersFrom(devJson) {
    const out = [];
    const gws = (devJson && devJson.data && Array.isArray(devJson.data.gwDevices)) ? devJson.data.gwDevices : [];
    for (const gw of gws) {
      const comm = Array.isArray(gw.commDevices) ? gw.commDevices : [];
      for (const dvc of comm) {
        const isInv = dvc.assetTypeName === "Inverter" || dvc.assetType === 2;
        if (!isInv) continue;
        const serial = String(dvc.sn || dvc.assetAlias || dvc.id || "").trim();
        if (!serial) continue;
        const powerW = num(dvc.currentPower);
        const st = mapStatus(dvc.statusName, powerW);
        // Honesty: don't ship a transient/partial 0 over a known-good live value.
        // A real off-state (offline/fault) legitimately reads 0 → keep it. An
        // unexplained 0/null while we just saw real watts is a mid-reload blip →
        // OMIT current_power_w so the backend retains the prior good reading
        // (its write at array_owners.py:2786 only fires when the field is present).
        let outPower = powerW;
        if (powerW != null && powerW > 0) {
          lastGoodPower.set(serial, powerW);
        } else if ((powerW == null || powerW === 0) && st !== "offline" && st !== "fault"
                   && lastGoodPower.has(serial)) {
          outPower = null;
        }
        out.push({
          serial,
          name: String(dvc.assetAlias || dvc.sn || serial),
          model: dvc.model || null,
          nameplate_kw: null,
          energy_today_kwh: num(dvc.eToday),
          current_power_w: outPower,
          status: st,
        });
      }
    }
    return out;
  }
  function countInverters(devJson) { return invertersFrom(devJson).length; }

  function broadcastLoginState(state) {
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED", provider: "chint",
      state, url: location.href, at: new Date().toISOString(),
    }, () => void chrome.runtime.lastError);
  }

  // Assemble whatever we've observed so far. Returns a payload or null if we
  // don't yet have the site list. Sites the owner hasn't opened simply have no
  // inverters yet (we keep them with site-level power so they still show up).
  function assemble() {
    // Build from the dashboard site list when we have it (richest: site names +
    // installed capacity). BUT also emit sites we only have DEVICE data for: a
    // user who lands on / deep-links to a site-detail page never triggers
    // /api/asset/site/retrieve, so requiring the list meant their inverters would
    // NEVER ship even though busTypeDevices loaded. Union both sources by site id.
    const listById = new Map();
    if (siteListJson && Array.isArray(siteListJson.data)) {
      for (const st of siteListJson.data) if (st && st.id != null) listById.set(String(st.id), st);
    }
    const ids = new Set();
    for (const k of listById.keys()) ids.add(k);
    for (const k of deviceJsonBySite.keys()) ids.add(String(k));
    if (!ids.size) return null;
    const sites = [];
    for (const sid of ids) {
      const st = listById.get(sid) || {};
      const name = st.siteName || (sid ? "Chint site " + sid : "Chint site");
      const devJson = deviceJsonBySite.has(sid) ? deviceJsonBySite.get(sid) : null;
      const inverters = devJson ? invertersFrom(devJson) : [];
      // Prefer the site's live power from the busTypeDevices response field
      // `currentPowerWithUnit` (e.g. "72.7 KW" -> 72700 W); the old numeric
      // `currentPower` field on the site list has drifted/gone missing.
      const liveFromDev = devJson && devJson.data ? parsePowerToW(devJson.data.currentPowerWithUnit) : null;
      const liveW = liveFromDev != null ? liveFromDev : num(st.currentPower);
      let energyToday = null;
      if (inverters.length) {
        energyToday = Math.round(inverters.reduce((t, iv) => t + (iv.energy_today_kwh || 0), 0) * 1000) / 1000;
      }
      // Best-effort site LOCATION for the weather model: deep-scan the responses we
      // ALREADY observed for this site — the site-list row first (richest), then the
      // busTypeDevices response. Purely additive; findLocation never throws → capture
      // is untouched if no coords are present. (Chint's own API can't be re-fetched
      // here — token replay 4010s — so we only scan what the app already loaded; the
      // exact coordinate field in these payloads is INFERRED, NEEDS a live verify.)
      let loc = null;
      try { loc = findLocation(st) || (devJson ? findLocation(devJson.data || devJson) : null); } catch (_) { loc = null; }
      const site = {
        site_id: String(sid != null ? sid : name),
        name,
        peak_power_kw: kwFromStr(st.installedCapacity),
        inverter_count: inverters.length || null,
        energy_today_kwh: energyToday,
        current_power_w: liveW,
        error_count_today: inverters.filter((iv) => iv.status === "fault").length,
        status: (liveW || 0) > 0 ? "producing" : "idle",
        daily: mergeDaily(weekTrendDaily(st), (sid != null && dailyBySite.has(String(sid))) ? dailyBySite.get(String(sid)) : []),   // 7-day weekETrend (site list) + chart-integrated history
        inverters,
      };
      applyLocation(site, loc);                     // additive latitude/longitude/address (best-effort)
      sites.push(site);
    }
    return { provider: "chint", capturedAt: new Date().toISOString(), sites };
  }

  function hasIntent() {
    return new Promise((res) => {
      try {
        chrome.storage.local.get(INTENT_KEY, (s) => {
          const it = s && s[INTENT_KEY];
          res(!!(it && it.vendor === "chint" && (Date.now() - (it.ts || 0)) < INTENT_TTL_MS));
        });
      } catch (_) { res(false); }
    });
  }
  function clearIntent() { try { chrome.storage.local.remove(INTENT_KEY); } catch (_) {} }
  function reportFailure(reason) {
    chrome.runtime.sendMessage(
      { type: "CHINT_CAPTURE_FAILED", reason: String(reason || "unknown"), url: location.href },
      () => void chrome.runtime.lastError,
    );
  }

  async function tick() {
    if (done) return;
    polls++;
    const intent = await hasIntent();
    if (!intent) { lastErr = "capture not requested from Array Operator (click “Log in with Chint” there first)"; return; }

    // First tick of this window: nudge the SPA to RE-FETCH its own data with its own
    // valid per-request token (replaying the token is banned — 4010). chint_inject.js
    // (MAIN world) does a hash-route bounce; a harmless same-route no-op if the SPA
    // already auto-refreshes. This is what makes the silent 4-min background cycle
    // produce FRESH readings instead of whatever the tab happened to load.
    // Keep nudging the SPA toward the sites list until we've actually OBSERVED it — NOT just once.
    // A first-time owner lands on the LOGIN page first; a one-shot poll-1 nudge fires while they're
    // still signing in (wasted), and after they log in nobody re-nudges → stuck on #/dashboard
    // forever (the exact bug Bruce hit). So re-fire every tick while we have NO site list yet AND
    // we're past the login form (no password field on screen — never navigate the page out from
    // under someone typing their password). Stops the instant the list lands (siteListJson → walk).
    if (!siteListJson && !document.querySelector('input[type="password"]')) {
      try { window.postMessage({ type: "SO_CHINT_FORCE_REFRESH" }, location.origin); } catch (_) {}
    }
    // AUTO-WALK (v1.9.77): once the site list is loaded, silently drive the SPA through
    // each site's detail route so its busTypeDevices fires — the click-free equivalent of
    // the owner opening every site. Confirmed live: a programmatic location.hash to
    // #/pv/sites/siteDetail/<id> fires busTypeDevices and our hooks observe it. The walk
    // itself runs in chint_inject.js (MAIN world); we just hand it the site ids we parsed.
    if (!_walkStarted && siteListJson && Array.isArray(siteListJson.data) && siteListJson.data.length) {
      const ids = siteListJson.data.map((s) => s && s.id).filter(Boolean).map(String);
      if (ids.length) {
        _walkStarted = true;
        _walkExpected = ids.length;
        try { window.postMessage({ type: "SO_CHINT_WALK_SITES", ids }, location.origin); } catch (_) {}
        LOG("auto-walk: driving the SPA through", ids.length, "site(s) to load inverters (no click needed)");
      }
    }

    const payload = assemble();
    if (!payload) {
      // Log the "waiting" state only once, not every tick (avoids console spam).
      if (!_warnedNoList) { LOG("waiting for Chint to load — open your dashboard / sites list on this tab"); _warnedNoList = true; }
      lastErr = "waiting for Chint to load — open your dashboard (site list) on this tab";
      broadcastLoginState("login_required");
      return;
    }
    broadcastLoginState("signed_in");

    const sites = payload.sites || [];
    const withInv = sites.filter((s) => (s.inverters || []).length > 0).length;
    const totalInv = sites.reduce((t, s) => t + (s.inverters || []).length, 0);
    // Hold out a few ticks for the owner to click into sites so we get per-inverter
    // data — but don't wait forever: after ~5 polls (~15s) with a site list but no
    // devices, ship what we have (site-level) so the flow completes honestly.
    if (withInv === 0 && polls < 6) {
      lastErr = "have site list, waiting for you to open a site so its inverters load";
      return;
    }

    // Signature includes EVERY site's inverters, so opening a NEW site changes the
    // hash and triggers a fresh emit. We do NOT stop after the first site — Bruce
    // (and any multi-site owner) opens sites one at a time, and each must be
    // captured. The backend upserts idempotently, so progressive re-emits are safe
    // and additive. We only stop on intent timeout (MAX_POLLS), never on "got one".
    // Signature includes EVERY site's inverters AND its captured daily-history
    // day-count, so opening a new site OR the production chart loading later both
    // change the hash and trigger a fresh emit (the chart often arrives AFTER the
    // inverters, so without the day-count the history would never ship).
    // Quantize live watts into 250 W bands so a GENUINE production change re-emits
    // (today's signature omits power entirely, so the live kW would never update),
    // while raw jitter on a plateau still suppresses the emit — no POST storm against
    // the un-rate-limited ingest endpoint.
    const qp = (w) => (w == null ? "_" : Math.round(w / 250) * 250);
    const sig = sites.map((s) =>
      s.site_id + "|" + (s.inverters || []).map((i) => i.serial + ":" + i.energy_today_kwh + ":" + qp(i.current_power_w)).join(",")
      + "|d" + ((s.daily || []).length)
    ).join("||");
    const h = await hashString(sig);
    if (h === lastHash) return;             // nothing new since last emit
    lastHash = h;
    LOG("EMIT payload:", sites.length, "site(s),", withInv, "with inverters,", totalInv, "inverters total");
    // Decisive diagnostic: dump each inverter's serial + live watts + today kWh so
    // the console alone tells us whether per-inverter power is flowing (the field
    // the backend needs for the "Output now" card).
    try {
      sites.forEach((s) => (s.inverters || []).forEach((iv) =>
        LOG("  inv", iv.serial, "power_w=", iv.current_power_w, "today_kwh=", iv.energy_today_kwh, "status=", iv.status)));
    } catch (_) {}
    // Progressive emit: ship the full current snapshot every time we learn about
    // more inverters (a newly-opened site). NEVER set done here — keep listening so
    // later site-opens are captured too. clearIntent stays armed until timeout.
    if (totalInv > 0) emittedAny = true;
    // walkComplete tells a silent recap/sync-all capture that the snapshot is FINAL (every
    // walked site visited) so it can close its surface — without it a multi-site owner is
    // truncated at site 1. True when the walk signalled done OR all expected sites have
    // inverters; for a non-walk (foreground) capture there's no recap surface to gate.
    payload.walkComplete = !_walkStarted ? true : (_walkDone || (_walkExpected > 0 && withInv >= _walkExpected));
    chrome.runtime.sendMessage({ type: "CHINT_CAPTURED", payload }, () => void chrome.runtime.lastError);
  }

  tick();
  const iv = setInterval(() => {
    if (done) { clearInterval(iv); return; }
    if (polls >= MAX_POLLS) {
      clearInterval(iv);
      // Only report failure if we NEVER captured anything. If we already emitted
      // real inverters (the progressive multi-site path), timing out is the normal
      // end of the capture window — stay silent so we don't overwrite a good result
      // with a spurious "failed" toast.
      if (!emittedAny) reportFailure(lastErr || "timed out with no data");
      else LOG("capture window ended — emitted", "inverters across the session");
      return;
    }
    tick();
  }, POLL_INTERVAL_MS);
})();
