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
  const POLL_INTERVAL_MS = 3000;
  const MAX_POLLS = 100;                       // ~5 min — owner needs time to click into sites
  let polls = 0;
  let lastHash = null;
  let lastLoginState = null;
  let lastErr = null;
  let done = false;
  let _warnedNoList = false;
  let emittedAny = false;

  // Observed response bodies, relayed by chint_inject.js (MAIN world).
  let siteListJson = null;                     // parsed /api/asset/site/retrieve
  const deviceJsonBySite = new Map();          // siteId -> parsed busTypeDevices

  function tryParse(body) { try { return JSON.parse(body); } catch (_) { return null; } }

  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin) return;
    const d = e.data;
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
        out.push({
          serial,
          name: String(dvc.assetAlias || dvc.sn || serial),
          model: dvc.model || null,
          nameplate_kw: null,
          energy_today_kwh: num(dvc.eToday),
          current_power_w: powerW,
          status: mapStatus(dvc.statusName, powerW),
        });
      }
    }
    return out;
  }
  function countInverters(devJson) { return invertersFrom(devJson).length; }

  // Chint's site/retrieve response carries weekETrend[] — the site's daily energy
  // for the last ~7 days: [{name:"20260610", value:"996.2"}, ...] (kWh). We map it
  // to a {date:"YYYY-MM-DD", kwh} series so the backend can backfill DailyGeneration
  // and the array graph renders REAL history the instant the owner connects, instead
  // of waiting days for the daily snapshot job to accumulate it. Site-level only —
  // Chint exposes no per-inverter history, so we never split this across inverters.
  function dailyFromTrend(st) {
    const trend = (st && Array.isArray(st.weekETrend)) ? st.weekETrend : [];
    const out = [];
    for (const pt of trend) {
      const raw = String((pt && pt.name) || "");
      const m = raw.match(/^(\d{4})(\d{2})(\d{2})$/);   // "20260610" -> 2026-06-10
      if (!m) continue;
      const kwh = Number(pt.value);
      if (!isFinite(kwh) || kwh < 0) continue;
      out.push({ date: m[1] + "-" + m[2] + "-" + m[3], kwh: Math.round(kwh * 1000) / 1000 });
    }
    return out;
  }

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
    if (!siteListJson || !Array.isArray(siteListJson.data) || !siteListJson.data.length) return null;
    const sites = [];
    for (const st of siteListJson.data) {
      const sid = st.id;
      const name = st.siteName || (sid ? "Chint site " + sid : "Chint site");
      const devJson = sid != null && deviceJsonBySite.has(String(sid))
        ? deviceJsonBySite.get(String(sid)) : null;
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
      sites.push({
        site_id: String(sid != null ? sid : name),
        name,
        peak_power_kw: kwFromStr(st.installedCapacity),
        inverter_count: inverters.length || null,
        energy_today_kwh: energyToday,
        current_power_w: liveW,
        error_count_today: inverters.filter((iv) => iv.status === "fault").length,
        status: (liveW || 0) > 0 ? "producing" : "idle",
        daily: dailyFromTrend(st),       // ~7 days of site daily kWh for instant history
        inverters,
      });
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
    const sig = sites.map((s) =>
      s.site_id + "|" + (s.inverters || []).map((i) => i.serial + ":" + i.energy_today_kwh).join(",")
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
