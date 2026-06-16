// chint_content.js — runs on monitor.chintpowersystems.com (Chint Power Systems
// / CPS "Monitor" portal — the real owner-facing monitoring site).
//
// Array Operator PER-INVERTER capture for CHINT / CPS. Like Fronius and SMA,
// Chint/CPS exposes no usable owner-facing API key, so this content script reads
// the owner's live readings straight from the logged-in portal's own JSON API
// and ships them to the AO ingest path.
//
// ── GROUNDED CONTRACT (HAR-captured against Bruce's live account, 2026-06-16) ──
// Inspected monitor.chintpowersystems.com while logged in as the GMCS Manager
// account (e.g. site "Londonderry 186", 186 kW, 4× SCA50KTL inverters). Every
// endpoint below is REAL and verified live — no guessing remains.
//
//   API BASE:  https://monitor.chintpowersystems.com:8443
//   AUTH:      custom request headers (NOT cookies):
//                token        ← localStorage["_token"]
//                loginuserid  ← localStorage["userIdByLogin"]
//                platformcode: 3
//                request-origin: web
//              CORS: the API answers ACAO=<page origin> (specific, not "*"), so
//              a content-script fetch works directly with the extension's
//              host_permissions — no background proxy needed (unlike SMA's uiapi).
//
//   1. whoami : GET /api/users/user/getUserInfo?appKey=WEB
//        → { code:"0", data:{ userId, email, userName } }
//   2. sites  : GET /api/asset/site/retrieve?page=1&limit=999&...(blank params)
//        → { code:"0", data:[ { id, siteName, installedCapacity(kW string),
//                               currentPower(W string), onlineCount, totalCount } ] }
//   3. devices: GET /api/asset/site/busTypeDevices?siteId=<id>
//        → { code:"0", data:{ gwDevices:[ { commDevices:[ {
//              assetTypeName:"Inverter", sn, assetAlias, model,
//              currentPower(W string), eToday(kWh number), statusName } ] } ] } }
//
// PRIVACY: capture runs ONLY when a recent, explicit AO "Connect Chint" click set
// the intent flag — never on a casual visit. SAFETY: read-only GETs; the token is
// read from the page's own localStorage and sent only to the Chint API; the
// extension persists nothing beyond the in-memory hand-off.

(function () {
  "use strict";
  if (!/(^|\.)chintpowersystems\.com$/.test(location.hostname)) return;

  const API = "https://monitor.chintpowersystems.com:8443";
  const INTENT_KEY = "so_capture_intent";
  const INTENT_TTL_MS = 10 * 60 * 1000;
  const POLL_INTERVAL_MS = 4000;
  const MAX_POLLS = 30;                       // ~2 min for the owner to finish signing in
  let polls = 0;
  let lastHash = null;
  let lastLoginState = null;
  let lastErr = null;                          // most recent failure reason, surfaced on give-up
  let done = false;

  async function hashString(s) {
    const buf = new TextEncoder().encode(String(s));
    const d = await crypto.subtle.digest("SHA-1", buf);
    return Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // Read a localStorage value, tolerating JSON-string wrapping (some SPA stores
  // JSON.stringify the raw token, leaving surrounding quotes).
  function lsRaw(key) {
    let v = null;
    try { v = window.localStorage.getItem(key); } catch (_) { return null; }
    if (v == null) return null;
    if (v.length >= 2 && v[0] === '"' && v[v.length - 1] === '"') {
      try { return JSON.parse(v); } catch (_) { return v.slice(1, -1); }
    }
    return v;
  }
  function authHeaders() {
    const token = lsRaw("_token");
    const uid = lsRaw("userIdByLogin");
    const h = {
      "Accept": "application/json, text/plain, */*",
      "platformcode": "3",
      "request-origin": "web",
    };
    if (token) h["token"] = token;
    if (uid) h["loginuserid"] = uid;
    return h;
  }
  function userId() { return lsRaw("userIdByLogin"); }

  // Credentialed-by-header GET against the Chint API. Cross-origin (page :443 →
  // API :8443) but the extension's host_permissions + the API's specific ACAO
  // make this work without a background proxy.
  async function getJson(path) {
    const url = API + path;
    const r = await fetch(url, { method: "GET", headers: authHeaders() });
    if (!r.ok) throw new Error(path + " -> " + r.status);
    const ct = r.headers.get("content-type") || "";
    if (!/json/i.test(ct)) throw new Error(path + " -> non-json (" + ct + ")");
    const j = await r.json();
    if (j && j.code != null && String(j.code) !== "0") {
      throw new Error(path + " -> api code " + j.code + " (" + (j.msg || "?") + ")");
    }
    return j;
  }

  function num(v) { const n = Number(v); return isFinite(n) ? n : null; }
  function kwFromStr(v) { const n = Number(v); return isFinite(n) ? Math.round(n * 1000) / 1000 : null; }

  // Map a Chint inverter status to our coarse states.
  function mapStatus(statusName, powerW) {
    const s = String(statusName || "").toLowerCase();
    if (/fault|error|alarm|warn/.test(s)) return "fault";
    if (/off|disconnect|standby|stop/.test(s)) return "offline";
    return (powerW || 0) > 0 ? "producing" : "idle";
  }

  // A logged-in portal answers getUserInfo with code:"0" + a userId. The login
  // page does not (401 / redirect / HTML).
  async function isSignedIn() {
    try {
      const who = await getJson("/api/users/user/getUserInfo?appKey=WEB");
      return !!(who && who.data && who.data.userId);
    } catch (_) {
      return false;
    }
  }
  function broadcastLoginState(state) {
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED", provider: "chint",
      state, url: location.href, at: new Date().toISOString(),
    }, () => void chrome.runtime.lastError);
  }

  async function captureFlow() {
    // 1) Site list — blank-param shape mirrors the portal's own call.
    const siteResp = await getJson(
      "/api/asset/site/retrieve?page=1&limit=999&key=&customerAdminId=&customerId=" +
      "&endUserId=&installerId=&acPlcSite=&projectId=&asc=true&sortKey=&selfParty="
    );
    const stations = Array.isArray(siteResp.data) ? siteResp.data : [];
    if (!stations.length) throw new Error("site list returned no sites");

    const sites = [];
    for (const st of stations) {
      const sid = st.id;
      const name = st.siteName || (sid ? "Chint site " + sid : "Chint site");
      const capacityKw = kwFromStr(st.installedCapacity);
      const liveW = num(st.currentPower);

      // 2) Per-site devices → inverters live under gwDevices[].commDevices[].
      let inverters = [];
      if (sid != null) {
        try {
          const devResp = await getJson("/api/asset/site/busTypeDevices?siteId=" + encodeURIComponent(sid));
          const gws = (devResp && devResp.data && Array.isArray(devResp.data.gwDevices)) ? devResp.data.gwDevices : [];
          for (const gw of gws) {
            const comm = Array.isArray(gw.commDevices) ? gw.commDevices : [];
            for (const dvc of comm) {
              const isInv = dvc.assetTypeName === "Inverter" || dvc.assetType === 2;
              if (!isInv) continue;
              const serial = String(dvc.sn || dvc.assetAlias || dvc.id || "").trim();
              if (!serial) continue;
              const powerW = num(dvc.currentPower);
              inverters.push({
                serial,
                name: String(dvc.assetAlias || dvc.sn || serial),
                model: dvc.model || null,
                nameplate_kw: null,                       // not exposed per-device by Chint
                energy_today_kwh: num(dvc.eToday),
                current_power_w: powerW,
                status: mapStatus(dvc.statusName, powerW),
              });
            }
          }
        } catch (e) {
          // Per-site device fetch failed — keep the site (with site-level power)
          // rather than dropping it; the comb just won't have per-inverter rows.
          lastErr = "device fetch failed for site " + sid + ": " + ((e && e.message) || e);
        }
      }

      // Site's energy-today = sum of its inverters' eToday (per-inverter truth).
      let energyToday = null;
      if (inverters.length) {
        energyToday = inverters.reduce((t, iv) => t + (iv.energy_today_kwh || 0), 0);
        energyToday = Math.round(energyToday * 1000) / 1000;
      }

      sites.push({
        site_id: String(sid != null ? sid : name),
        name,
        peak_power_kw: capacityKw,
        inverter_count: inverters.length || null,
        energy_today_kwh: energyToday,
        current_power_w: liveW,
        error_count_today: inverters.filter((iv) => iv.status === "fault").length,
        status: (liveW || 0) > 0 ? "producing" : "idle",
        inverters,
      });
    }
    if (!sites.length) throw new Error("no usable sites parsed");
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
    if (!(await hasIntent())) { lastErr = "capture not requested from Array Operator (click “Log in with Chint” there first)"; return; }
    // Auth comes from localStorage — if it's not there yet the owner hasn't
    // finished signing in (the SPA writes _token/userIdByLogin post-login).
    if (!lsRaw("_token") || !userId()) { lastErr = "not signed in to Chint yet (no session token on the page)"; broadcastLoginState("login_required"); return; }
    let signedIn;
    try { signedIn = await isSignedIn(); }
    catch (e) { lastErr = "auth-check failed: " + ((e && e.message) || e); return; }
    if (!signedIn) { lastErr = "not signed in to Chint Monitor yet"; broadcastLoginState("login_required"); return; }
    broadcastLoginState("signed_in");
    let payload;
    try { payload = await captureFlow(); }
    catch (e) { lastErr = "capture failed: " + ((e && e.message) || e); return; }   // retry next tick
    const sites = payload.sites || [];
    if (!sites.length) { lastErr = "signed in, but no sites/inverters returned"; return; }
    const sig = sites.map((s) =>
      s.site_id + "|" + (s.inverters || []).map((i) => i.serial + ":" + i.energy_today_kwh).join(",")
    ).join("||");
    const h = await hashString(sig);
    if (h === lastHash) return;
    lastHash = h;
    done = true;
    clearIntent();
    chrome.runtime.sendMessage({ type: "CHINT_CAPTURED", payload }, () => void chrome.runtime.lastError);
  }

  tick();
  const iv = setInterval(() => {
    if (done) { clearInterval(iv); return; }
    if (polls >= MAX_POLLS) {
      clearInterval(iv);
      reportFailure(lastErr || "timed out with no data");
      return;
    }
    tick();
  }, POLL_INTERVAL_MS);
})();
