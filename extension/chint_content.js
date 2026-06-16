// chint_content.js — runs on solar.chintpower.com (Chint Connect / CPS portal).
//
// Array Operator PER-INVERTER capture for CHINT / CPS. Like Fronius and SMA,
// Chint/CPS exposes no usable owner-facing API key (the cloud is a Fomware-built
// white-label — contact on the portal's privacy page is how@fomware.com, and
// CHINT publishes no public API docs). So, exactly like the Fronius/SMA paths,
// this content script reads the owner's live readings straight from the logged-in
// portal's own JSON endpoints (first-party session, credentials:"include") and
// ships them to the AO ingest path.
//
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║ LOUD CAVEAT — NOT YET GROUNDED AGAINST A LIVE CHINT ACCOUNT.               ║
// ║                                                                            ║
// ║ SolarEdge, Fronius and SMA were each grounded against a real login (HAR    ║
// ║ capture → verified endpoints). We have NO live CHINT account to inspect,   ║
// ║ and CHINT/CPS ship no API docs. The CANDIDATE_* endpoints below are        ║
// ║ best-effort guesses for the Fomware portal's internal API — they are       ║
// ║ UNVERIFIED. The script is written to FAIL GRACEFULLY: if none of the       ║
// ║ candidates return usable data it reports CHINT_CAPTURE_FAILED with a clear ║
// ║ reason (the AO spinner resolves honestly — it never hangs and never        ║
// ║ pretends success). To finish CHINT: open solar.chintpower.com logged in,   ║
// ║ grab the station-list + device-list XHRs from DevTools/HAR, and replace    ║
// ║ the CANDIDATE_* lists + mapping below with the real paths/shapes.          ║
// ╚══════════════════════════════════════════════════════════════════════════╝
//
// PRIVACY: capture runs ONLY when a recent, explicit AO "Connect Chint" click set
// the intent flag — never on a casual visit. SAFETY: read-only GETs; the
// extension persists nothing beyond the in-memory hand-off.

(function () {
  "use strict";
  if (!/(^|\.)chintpower\.com$/.test(location.hostname)) return;

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
  // Same-origin credentialed GET (the portal SPA's session cookie rides). If
  // grounding reveals the Chint API lives on a DIFFERENT host with ACAO:* (as
  // SMA's uiapi did), route these through a background CHINT_API_GET proxy the
  // way sunnyportal_content.js does — add it then.
  async function getJson(url) {
    const r = await fetch(url, { credentials: "include", headers: { "Accept": "application/json" } });
    if (!r.ok) throw new Error(url + " -> " + r.status);
    const ct = r.headers.get("content-type") || "";
    if (!/json/i.test(ct)) throw new Error(url + " -> non-json (" + ct + ")");
    return r.json();
  }
  // Try a list of CANDIDATE endpoints; return the first that yields JSON. Honest
  // about being a probe — every candidate is wrapped so a 404/login-redirect just
  // moves to the next, and an all-miss is reported, not swallowed.
  async function firstJson(candidates) {
    for (const path of candidates) {
      try { return { path, data: await getJson(path) }; } catch (_) { /* next candidate */ }
    }
    return null;
  }

  // ── CANDIDATE endpoints — UNVERIFIED guesses for the Fomware/Chint Connect API.
  // Replace with the real paths from a live-account HAR capture. Kept same-origin
  // (relative) so the page's session cookie authenticates them.
  const CANDIDATE_WHOAMI = [
    "/maxweb/proxy/account/getUserInfo",
    "/proxyApp/proxy/api/user/info",
    "/api/user/info",
    "/cps/api/user/current",
  ];
  const CANDIDATE_STATION_LIST = [
    "/maxweb/proxy/station/getStationList",
    "/proxyApp/proxy/api/station/list",
    "/api/station/list",
    "/cps/api/plant/list",
  ];
  // Per-station device list; {sid} is replaced with the station id.
  const CANDIDATE_DEVICE_LIST = [
    "/maxweb/proxy/device/getDeviceList?stationId={sid}",
    "/proxyApp/proxy/api/device/list?stationId={sid}",
    "/api/device/list?stationId={sid}",
    "/cps/api/inverter/list?plantId={sid}",
  ];

  // Defensive field pluckers — these portals vary the casing/keys, so reach for
  // several common names before giving up on a value.
  function pick(obj, keys) {
    for (const k of keys) { if (obj && obj[k] != null) return obj[k]; }
    return null;
  }
  function asArray(resp) {
    if (Array.isArray(resp)) return resp;
    // Common envelope shapes: {data:[...]}, {data:{records:[...]}}, {result:{list:[...]}}
    const d = resp && (resp.data || resp.result || resp.records || resp.list);
    if (Array.isArray(d)) return d;
    if (d && Array.isArray(d.records)) return d.records;
    if (d && Array.isArray(d.list)) return d.list;
    if (d && Array.isArray(d.rows)) return d.rows;
    return [];
  }
  function toKw(v) {
    const n = Number(v);
    return isFinite(n) ? Math.round(n * 10) / 10 : null;
  }

  // A logged-in portal answers one of the whoami candidates with JSON. The login
  // page does not (it redirects/returns HTML). Also treat being off the index/
  // login route as a weak positive so a fresh dashboard load isn't a false miss.
  async function isSignedIn() {
    const who = await firstJson(CANDIDATE_WHOAMI);
    if (who) return true;
    // No whoami candidate matched — fall back to: are we past the login screen?
    const p = location.pathname.toLowerCase();
    const onLogin = /login|signin|index(en)?\.html$|^\/$/.test(p);
    return !onLogin;   // best-effort; capture still has to find a station list to succeed
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
    const stationResp = await firstJson(CANDIDATE_STATION_LIST);
    if (!stationResp) throw new Error("no station-list endpoint responded (chint endpoints unverified — needs live-account grounding)");
    const stations = asArray(stationResp.data);
    if (!stations.length) throw new Error("station list returned no stations");

    const sites = [];
    for (const st of stations) {
      const sid = pick(st, ["stationId", "id", "plantId", "stationGuid", "uid"]);
      const name = pick(st, ["stationName", "name", "plantName", "title"]) || (sid ? "Chint station " + sid : "Chint station");
      const capacityKw = toKw(pick(st, ["capacity", "installedCapacity", "nameplate", "totalPower", "kwp"]));
      const liveW = (() => { const n = Number(pick(st, ["currentPower", "power", "acPower"])); return isFinite(n) ? n : null; })();
      const energyToday = (() => { const n = Number(pick(st, ["todayEnergy", "energyToday", "dayEnergy", "etoday"])); return isFinite(n) ? n : null; })();

      // Per-inverter drill-down (best-effort — the site still ships without it).
      let inverters = [];
      if (sid != null) {
        const devResp = await firstJson(CANDIDATE_DEVICE_LIST.map((p) => p.replace("{sid}", encodeURIComponent(sid))));
        const devs = devResp ? asArray(devResp.data) : [];
        inverters = devs.map((d, i) => ({
          serial: String(pick(d, ["sn", "serial", "deviceSn", "deviceId", "id"]) || (sid + "-" + i)),
          name: String(pick(d, ["deviceName", "name", "alias"]) || ("Inverter " + (i + 1))),
          model: pick(d, ["deviceType", "model", "type"]) || null,
          nameplate_kw: toKw(pick(d, ["capacity", "ratedPower", "nameplate"])),
          energy_today_kwh: (() => { const n = Number(pick(d, ["todayEnergy", "energyToday", "etoday"])); return isFinite(n) ? n : null; })(),
          current_power_w: (() => { const n = Number(pick(d, ["power", "acPower", "currentPower"])); return isFinite(n) ? n : null; })(),
          status: (() => {
            const s = String(pick(d, ["status", "state", "runState"]) || "").toLowerCase();
            if (/fault|error|alarm/.test(s)) return "fault";
            if (/off|disconnect/.test(s)) return "offline";
            const p = Number(pick(d, ["power", "acPower", "currentPower"]));
            return isFinite(p) && p > 0 ? "producing" : "idle";
          })(),
        }));
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
    if (!sites.length) throw new Error("no usable stations parsed");
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
    let signedIn;
    try { signedIn = await isSignedIn(); }
    catch (e) { lastErr = "auth-check failed: " + ((e && e.message) || e); return; }
    if (!signedIn) { lastErr = "not signed in to Chint Connect yet"; broadcastLoginState("login_required"); return; }
    broadcastLoginState("signed_in");
    let payload;
    try { payload = await captureFlow(); }
    catch (e) { lastErr = "capture failed: " + ((e && e.message) || e); return; }   // retry next tick
    const sites = payload.sites || [];
    if (!sites.length) { lastErr = "signed in, but no stations/inverters returned"; return; }
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
      // Out of retries with no capture — tell the AO page WHY (honest, ungrounded)
      // instead of leaving its spinner hanging forever.
      reportFailure(lastErr || "timed out with no data (chint endpoints not yet verified)");
      return;
    }
    tick();
  }, POLL_INTERVAL_MS);
})();
