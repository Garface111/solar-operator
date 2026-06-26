// smarthub_content.js — universal NISC SmartHub content script.
//
// Runs on ALL *.smarthub.coop pages. Replaces vec_content.js with a
// host-aware version that works for VEC, WEC, Stowe, and any future
// SmartHub utility in the registry.
//
// Three things this script does:
//   1. Detects which SmartHub utility is open (via smarthub_registry.js)
//   2. Intercepts the SmartHub login API response to capture the
//      authorizationToken — enables server-side generation pulls
//   3. Scrapes billing history and usage-explorer DOM (same as vec_content.js)
//
// Backward compatibility: VEC sessions already in flight use the same
// scraping logic. The provider code in the payload changes from the old
// hardcoded "vec" to the host-detected code — still "vec" for VEC.
//
// Message types sent to background.js:
//   SMARTHUB_DATA_CAPTURED  — billing/usage scrape result (maps to postSync)
//   LOGIN_STATE_DETECTED    — login state broadcast for onboarding wizard

(function () {
  "use strict";

  // smarthub_registry.js is loaded before this script (see manifest.json).
  // window.detectSmartHubProvider is set by that script.
  const registryEntry =
    typeof window.detectSmartHubProvider === "function"
      ? window.detectSmartHubProvider(location.hostname)
      : null;

  if (!registryEntry) {
    // Not a SmartHub host — should not happen given manifest matches, but guard.
    return;
  }

  const PROVIDER = registryEntry.provider; // e.g. "vec", "wec"
  const UTILITY_NAME = registryEntry.name;

  const LOG = (...a) => { try { console.log(`[EnergyAgent ${UTILITY_NAME}]`, ...a); } catch (_) {} };

  const POLL_INTERVAL_MS = 2000;
  const MAX_POLLS = 30; // 60 seconds before giving up

  let pollCount = 0;
  let lastSentHash = null;

  // ─── Auth token interception ─────────────────────────────────────────────
  // Monkey-patch fetch to intercept the SmartHub login API response and
  // extract the authorizationToken. This enables server-side generation pulls
  // without requiring the operator to enter their password in EnergyAgent.
  //
  // Only intercepts /services/oauth/auth/v2. All other requests are untouched.

  let capturedAuthToken = null;
  let capturedPrimaryUsername = null;

  (function patchFetch() {
    const _origFetch = window.fetch;
    window.fetch = async function (...args) {
      const req = args[0];
      const url = typeof req === "string" ? req : req instanceof URL ? req.toString() : (req.url || "");
      const resp = await _origFetch.apply(this, args);
      if (url.includes("/services/oauth/auth/v2")) {
        try {
          const clone = resp.clone();
          clone.json().then((data) => {
            const token = data.authorizationToken || data.authorization_token;
            const username = data.primaryUsername || data.primary_username;
            if (token) {
              capturedAuthToken = token;
              capturedPrimaryUsername = username || null;
              // v1.9.25: Array-Operator meter-production capture. When the AO
              // page armed a meter intent for THIS SmartHub provider (vec/wec),
              // hand the short-lived SmartHub session to the backend so it can
              // pull daily generation server-side. ADDITIVE — the bill capture
              // (SMARTHUB_DATA_CAPTURED) path below is untouched and still runs.
              maybeSendMeterCapture();
            }
          }).catch(() => {});
        } catch (_) {}
      }
      return resp;
    };
  })();

  // ─── Array-Operator meter-production capture (v1.9.26 — CLIENT-SIDE pull) ──
  // Gated on an armed AO meter intent (chrome.storage.local so_capture_intent
  // {vendor:<provider>}). The SmartHub data API authenticates with the owner's
  // httpOnly SESSION COOKIE — which the backend CANNOT replay (the v1.9.25
  // server-side-pull design was wrong; proven by the live VEC HAR). So we pull
  // the daily generation HERE, same-origin (credentials:"include" rides the
  // cookie), assemble the per-account daily[] series the backend already ingests
  // (utility-meter-capture, the proven GMP path), and ship it via background →
  // SO_CAPTURE_LANDED. GROUNDED on West Glover (vermontelectric, acct 6578300):
  //   GET  /services/secured/user-data          → accounts + serviceLocation + address
  //   POST /services/secured/utility-usage       → {ELECTRIC:[{series:[{data:[{x,y}]}]}]}
  //   NEGATIVE daily y = net export = generation (regardless of meter flags).
  let meterCaptureSent = false;

  function meterIntentArmed() {
    return new Promise((resolve) => {
      try {
        chrome.storage.local.get(["so_capture_intent", "tenant_key"], (res) => {
          if (chrome.runtime.lastError) { void chrome.runtime.lastError; return resolve(false); }
          // (a) Array Operator path: an explicit vec/wec meter intent armed when
          //     the owner clicked "Connect" on the AO site (10-min TTL).
          const intent = res && res.so_capture_intent;
          const aoArmed = !!(intent && intent.vendor === PROVIDER &&
            (Date.now() - (intent.ts || 0)) < 10 * 60 * 1000);
          // (b) NEPOOL path: the extension is paired to a tenant (tenant_key set).
          //     SmartHub bills carry NO generation kWh, so a NEPOOL operator's
          //     reports stay empty unless we ALSO pull daily generation here.
          //     There's no AO intent on the NEPOOL side, so being paired is the
          //     trigger — the background routes the result to the dual-auth
          //     utility-meter-capture endpoint with the stored tenant_key.
          const paired = !!(res && res.tenant_key);
          resolve(aoArmed || paired);
        });
      } catch (_) { resolve(false); }
    });
  }

  // Resolve the SmartHub username/email needed by user-data + utility-usage.
  // SmartHub passes it BOTH as a ?userId= query param (user-data) AND as the
  // x-nisc-smarthub-username header (both calls) — grounded on Paul's VEC HAR.
  // Source it the same way the working bill capture does: the home-page URL hash
  // (#/home?<base64 ...userId=...>), then the auth-intercept username, as fallbacks.
  function resolveUsername() {
    // 1) Auth-intercept captured it from a login/refresh response (most reliable).
    if (capturedPrimaryUsername) return capturedPrimaryUsername;
    // 2) Home-page URL hash: #/home?<base64 ...userId=...>.
    try {
      const creds = decodeHashCreds();
      if (creds && creds.userId) return creds.userId;
    } catch (_) {}
    // 3) SmartHub caches the login in web storage — scan for an email-looking value
    //    under common NISC keys (verified key names vary across deployments).
    try {
      for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i++) {
          const k = store.key(i);
          const v = store.getItem(k) || "";
          // direct email value
          const m = v.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
          if (m && /user|name|login|email|primary|nisc|smarthub/i.test(k)) return m[0];
          // JSON blob with a username/primaryUsername field
          if (v && (v[0] === "{" || v[0] === "[")) {
            try {
              const j = JSON.parse(v);
              const cand = j && (j.primaryUsername || j.username || j.userId || j.email);
              if (cand && /@/.test(cand)) return cand;
            } catch (_) {}
          }
        }
      }
    } catch (_) {}
    return null;
  }

  // Same-origin authenticated GET/POST — the session cookie rides automatically.
  // SmartHub additionally requires the x-nisc-smarthub-username header on secured
  // data calls (verified: without it user-data 401s → "couldn't read accounts").
  async function shGet(path, username) {
    const headers = { "Accept": "application/json" };
    if (username) headers["x-nisc-smarthub-username"] = username;
    const r = await fetch(path, { credentials: "include", headers });
    if (!r.ok) { const e = new Error("HTTP " + r.status); e.status = r.status; throw e; }
    return r.json();
  }
  async function shPost(path, body, username) {
    const headers = { "Content-Type": "application/json", "Accept": "application/json" };
    if (username) headers["x-nisc-smarthub-username"] = username;
    const r = await fetch(path, {
      method: "POST", credentials: "include", headers,
      body: JSON.stringify(body),
    });
    if (!r.ok) { const e = new Error("HTTP " + r.status); e.status = r.status; throw e; }
    return r.json();
  }

  // How far back to pull daily generation. The NEPOOL/GMCS report renders 6
  // rolling quarters (18 months); we pull ~19 months so a SINGLE owner re-login
  // backfills the entire reporting window — not just the last month (the old
  // 35-day window left every historical quarter at zero). Chunked because a
  // single 18-month DAILY POST gets truncated/rejected by the NISC API; 90-day
  // chunks are the proven-safe size for the utility-usage endpoint.
  const GEN_LOOKBACK_DAYS = 580;   // ~19 months — covers 6 quarters + margin
  const GEN_CHUNK_DAYS = 90;       // safe per-request DAILY window

  // Reduce ONE utility-usage response into per-day generation, accumulating into
  // `byDay` (isoDay -> generated kWh). Grounded contract:
  //   NEGATIVE daily y = net export = generation (West Glover's meter is tagged
  //   FORWARD/isNetMeter=false yet net-exports). An explicit RETURN flow, if
  //   present, is generation directly.
  function _reduceUsageInto(data, byDay) {
    const electric = (data && Array.isArray(data.ELECTRIC)) ? data.ELECTRIC : [];
    for (const entry of electric) {
      const seriesMap = {};
      for (const s of (entry.series || [])) {
        const name = (s.name != null ? s.name : s.seriesId);
        if (name != null) seriesMap[String(name)] = s.data || [];
      }
      let meters = entry.meters || [];
      if (!meters.length && Object.keys(seriesMap).length) {
        meters = Object.keys(seriesMap).map((k) => ({ seriesId: k, flowDirection: "NET" }));
      }
      for (const m of meters) {
        const sid = String(m.seriesId || m.meterName || "");
        const flow = String(m.flowDirection || "").toUpperCase();
        let pts = seriesMap[sid] || [];
        if (!pts.length && Object.keys(seriesMap).length === 1) pts = Object.values(seriesMap)[0];
        for (const pt of pts) {
          const x = pt.x, y = pt.y;
          if (x == null || y == null) continue;
          // Guard against a malformed timestamp: new Date(bad).toISOString()
          // throws RangeError("Invalid time value") and would abort the whole
          // capture. Skip the unparseable point instead of crashing.
          const dt = new Date(x);
          if (isNaN(dt.getTime())) continue;
          const day = dt.toISOString().slice(0, 10);
          const kwh = Number(y);
          if (!Number.isFinite(kwh)) continue;  // non-numeric reading -> skip
          let gen = 0;
          if (flow === "RETURN") gen = Math.max(0, kwh);
          else if (kwh < 0) gen = Math.abs(kwh);
          if (gen > 0) byDay[day] = (byDay[day] || 0) + gen;
        }
      }
    }
  }

  // Pull one chunk [start,end] of DAILY usage for (account, serviceLocation).
  // Returns the raw response or null on failure (cookie-only first, then retry
  // with the nisc username header for deployments that require it).
  async function _fetchUsageChunk(userId, accountNumber, serviceLocation, start, end) {
    const body = {
      timeFrame: "DAILY",
      userId: userId,
      screen: "USAGE_COMPARISON",
      includeDemand: false,
      serviceLocationNumber: String(serviceLocation),
      accountNumber: String(accountNumber),
      industries: ["ELECTRIC"],
      startDateTime: start.getTime(),
      endDateTime: end.getTime(),
    };
    try { return await shPost("/services/secured/utility-usage", body); }
    catch (e1) {
      try { return await shPost("/services/secured/utility-usage", body, userId); }
      catch (e2) {
        LOG("usage chunk failed for", accountNumber,
          start.toISOString().slice(0, 10), "→", end.toISOString().slice(0, 10),
          e1 && e1.message, "/", e2 && e2.message);
        return null;
      }
    }
  }

  // Pull the FULL reporting window of daily generation in 90-day chunks and
  // reduce the negative-y export signal into per-day generation rows.
  async function fetchDailyGeneration(userId, accountNumber, serviceLocation) {
    const overallEnd = new Date();
    const overallStart = new Date(overallEnd.getTime() - GEN_LOOKBACK_DAYS * 24 * 3600 * 1000);
    const byDay = {};   // isoDay -> generated kWh
    const chunkMs = GEN_CHUNK_DAYS * 24 * 3600 * 1000;
    let okChunks = 0, failChunks = 0;
    // Walk newest→oldest so partial failures still leave the most recent
    // (most-likely-needed) data populated.
    let chunkEnd = new Date(overallEnd.getTime());
    while (chunkEnd > overallStart) {
      const chunkStart = new Date(Math.max(overallStart.getTime(), chunkEnd.getTime() - chunkMs));
      const data = await _fetchUsageChunk(userId, accountNumber, serviceLocation, chunkStart, chunkEnd);
      if (data) { _reduceUsageInto(data, byDay); okChunks++; }
      else { failChunks++; }
      // step back one day past chunkStart to avoid double-counting the boundary
      chunkEnd = new Date(chunkStart.getTime() - 24 * 3600 * 1000);
    }
    LOG("daily generation for", accountNumber, "→", Object.keys(byDay).length,
      "day(s) over", okChunks, "ok /", failChunks, "failed chunk(s)");
    return Object.keys(byDay).sort().map((d) => ({ date: d, generated_kwh: Math.round(byDay[d] * 1000) / 1000 }));
  }

  async function maybeSendMeterCapture() {
    if (meterCaptureSent) return;
    if (!(await meterIntentArmed())) return;
    meterCaptureSent = true;
    LOG("meter intent armed — pulling daily generation client-side");
    try {
      // Discover accounts the SAME WAY the WORKING bill capture does — via
      // /services/secured/billing/history/overview (cookie-only, NO nisc header,
      // proven 200 in the live test). The 401-ing /user-data call is GONE. The
      // overview response carries everything the usage call needs per account:
      //   acctNbr, custNbr, servLocs[0].id.srvLocNbr (service location), address.
      const creds = decodeHashCreds();
      const sessionUser = resolveUsername();   // for the usage body userId + (fallback) header
      const domAccts = acctsFromDom();
      const acctNbrs = new Set(domAccts.keys());
      if (creds && creds.acctNbr) acctNbrs.add(creds.acctNbr);
      if (acctNbrs.size === 0) {
        LOG("no accounts discoverable yet — will retry next scrape");
        meterCaptureSent = false;
        return;
      }

      const accounts = [];
      for (const acctNbr of acctNbrs) {
        let overview;
        try {
          const res = await fetch(
            `/services/secured/billing/history/overview?acctNbr=${encodeURIComponent(acctNbr)}`,
            { credentials: "include", headers: { "Accept": "application/json" } }
          );
          if (!res.ok) { LOG("overview failed for", acctNbr, "HTTP " + res.status); continue; }
          overview = await res.json();
        } catch (e) { LOG("overview error for", acctNbr, e && e.message); continue; }

        const rowsArr = Array.isArray(overview) ? overview : (overview ? [overview] : []);
        const first = rowsArr[0] || {};
        const sl = (first.servLocs && first.servLocs[0]) || {};
        const srvLocNbr = (sl.id && (sl.id.srvLocNbr || sl.id.serviceLocation)) || null;
        const addr = sl.address || {};
        const addrStr = [addr.addr1, addr.city, addr.state].filter(Boolean).join(", ") ||
          domAccts.get(acctNbr) || `${PROVIDER.toUpperCase()} ${acctNbr}`;
        if (srvLocNbr == null) { LOG("no service location for", acctNbr, "— skipping usage pull"); continue; }

        const daily = await fetchDailyGeneration(sessionUser || "", acctNbr, srvLocNbr);
        accounts.push({ account_number: String(acctNbr), nickname: addrStr, summary: {}, daily });
      }

      if (accounts.length === 0) {
        chrome.runtime.sendMessage({ type: "SMARTHUB_METER_FAILED", provider: PROVIDER,
          reason: "couldn't read your accounts — try again" }, () => void chrome.runtime.lastError);
        return;
      }
      const totalGenDays = accounts.reduce((t, a) => t + a.daily.length, 0);
      LOG("EMIT meter capture:", accounts.length, "account(s),", totalGenDays, "generation day(s)");
      chrome.runtime.sendMessage({
        type: "SMARTHUB_METER_GEN_CAPTURED",
        provider: PROVIDER,
        accounts,
      }, () => void chrome.runtime.lastError);
    } catch (e) {
      LOG("meter capture error", e && e.message);
      chrome.runtime.sendMessage({ type: "SMARTHUB_METER_FAILED", provider: PROVIDER,
        reason: "couldn't read your production data" }, () => void chrome.runtime.lastError);
    }
  }

  // ─── Utility helpers ─────────────────────────────────────────────────────

  async function hashString(s) {
    const buf = new TextEncoder().encode(s);
    const digest = await crypto.subtle.digest("SHA-1", buf);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }

  // ─── Billing history scraper ─────────────────────────────────────────────
  // Columns (0-indexed): Account# | AutoPay | CustomerName | Address |
  //   BillingDate | BillAmount | Adjustments | TotalDue | [ViewBill link]

  function parseBillingHistory() {
    const rows = [];
    const trs = document.querySelectorAll("table tr, mat-row");
    for (const tr of trs) {
      const cells = tr.querySelectorAll("td, mat-cell");
      if (cells.length === 0) continue;

      // ── Layout B (WEC + newer NISC responsive tables, June 2026) ──────
      // 5 mat-cells with data-label attributes:
      //   Account | Billing Date | Paperless (amount + View Bill) |
      //   Adjustments | Total Due
      // "View Bill" is an Angular click handler (a.view-bill-pdf, NO href).
      const byLabel = {};
      for (const c of cells) {
        const label = (c.getAttribute("data-label") || "").trim().toLowerCase();
        if (label) byLabel[label] = c;
      }

      let maybeDate, accountId, customerName, serviceAddress;
      let billAmountText, adjustmentsText, totalDueText;

      if (byLabel["billing date"]) {
        const dateText = (byLabel["billing date"].textContent || "").trim();
        const dm = dateText.match(/\d{1,2}\/\d{1,2}\/\d{4}/);
        if (!dm) continue;
        maybeDate = dm[0];

        const acctCell = byLabel["account"];
        const acctText = acctCell ? acctCell.innerText || "" : "";
        // "ELECTRIC SERVICE — 982501" (em-dash or hyphen)
        const am = acctText.match(/[—\-–]\s*(\d{4,})/);
        accountId = am ? am[1] : "";
        // Account cell lines: header, service—acct, AutoPay, NAME, ADDRESS, View Usage
        const lines = acctText.split("\n").map((s) => s.trim()).filter(Boolean);
        customerName =
          lines.find((l) => /^[A-Z][A-Z .'-]+$/.test(l) && !/SERVICE|ACCOUNT|AUTO ?PAY|VIEW|PAPERLESS/i.test(l)) || "";
        serviceAddress =
          lines.find((l) => /\d+.*(,|RD|ROAD|ST|STREET|AVE|LN|DR|VT|NH|MA)\b/i.test(l) && l !== customerName) || "";

        const amtCell = byLabel["paperless"] || byLabel["bill amount"];
        billAmountText = amtCell ? (amtCell.innerText.match(/\$[\d,.\-]+/) || [""])[0] : "";
        adjustmentsText = byLabel["adjustments"] ? byLabel["adjustments"].innerText : "";
        totalDueText = byLabel["total due"] ? byLabel["total due"].innerText : "";
      } else {
        // ── Layout A (legacy VEC 8-column flat table) ──────────────────
        if (cells.length < 8) continue;
        maybeDate = (cells[4]?.textContent || "").trim();
        if (!/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(maybeDate)) continue;
        accountId = (cells[0]?.textContent || "").trim();
        customerName = (cells[2]?.textContent || "").trim();
        serviceAddress = (cells[3]?.textContent || "").trim();
        billAmountText = cells[5]?.textContent || "";
        adjustmentsText = cells[6]?.textContent || "";
        totalDueText = cells[7]?.textContent || "";
      }

      const link = tr.querySelector("a[href*='billPdfService']");
      let pdfUrl = link ? link.href : null;
      let billUuid = null;
      let billTimestamp = null;

      if (pdfUrl) {
        // Pull params by regex first so a malformed pdfUrl (which makes
        // `new URL()` throw) never drops a valid account number / uuid.
        const qParam = (name) => {
          const m = pdfUrl.match(new RegExp("[?&]" + name + "=([^&#]*)"));
          return m ? decodeURIComponent(m[1]) : null;
        };
        billUuid = qParam("uuid");
        billTimestamp = qParam("timestamp");
        if (!accountId) accountId = qParam("account") || "";
        try {
          // Prefer the spec-correct URL parser when the href is well-formed.
          const u = new URL(pdfUrl);
          billUuid = u.searchParams.get("uuid") ?? billUuid;
          billTimestamp = u.searchParams.get("timestamp") ?? billTimestamp;
          if (!accountId) accountId = u.searchParams.get("account") || "";
        } catch (_) {}
      }

      rows.push({
        account_id: accountId,
        customer_name: (customerName || "").trim(),
        service_address: (serviceAddress || "").trim(),
        billing_date: maybeDate,
        bill_amount: (billAmountText || "").replace(/[^0-9.\-]/g, ""),
        adjustments: (adjustmentsText || "").replace(/[^0-9.\-]/g, ""),
        total_due: (totalDueText || "").replace(/[^0-9.\-]/g, ""),
        pdf_url: pdfUrl,
        bill_uuid: billUuid,
        bill_timestamp: billTimestamp,
      });
    }
    return rows;
  }

  // ─── Usage explorer scraper ──────────────────────────────────────────────
  // aria-label format (NISC SmartHub SPA — uniform across all deployments):
  //   "Jun 2023 Billing Period. Usage Dates: May 18 - June 17.
  //    Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F"

  // Two observed shapes: VEC "Meter N - Consumption - kWh: X kWh" and
  // WEC "Meter N - kWh: X kWh" — the middle type segment is optional.
  const ARIA_RE =
    /^([^\n.]+?)\s+Billing Period\.\s+Usage Dates:\s+([^\n.]+?)\s*\.[\s\S]+?Meter\s+(\d+)\s+-\s+(?:[^\n\-]+?\s+-\s+)?kWh:\s+([\d,.]+)\s+kWh(?:[\s\S]*?Average Temperature:\s+([\d.]+)\s*°?F)?/i;

  function parseUsageExplorer() {
    const rows = [];
    const images = document.querySelectorAll("image[aria-label], svg image[aria-label]");
    for (const img of images) {
      const label = (img.getAttribute("aria-label") || "").trim();
      const m = ARIA_RE.exec(label);
      if (!m) continue;
      rows.push({
        period_label: m[1].trim(),
        usage_dates_raw: m[2].trim(),
        meter_id: m[3],
        kwh: parseFloat(m[4]),
        avg_temp_f: m[5] ? parseFloat(m[5]) : null,
      });
    }
    return rows;
  }

  // ─── Account list from billing rows ─────────────────────────────────────

  function extractAccounts(bills) {
    const seen = new Map();
    for (const b of bills) {
      if (b.account_id && !seen.has(b.account_id)) {
        seen.set(b.account_id, {
          accountNumber: b.account_id,
          customerName: b.customer_name,
          serviceAddress: b.service_address,
        });
      }
    }
    return [...seen.values()];
  }

  // ─── Page detection ──────────────────────────────────────────────────────

  function isOnBillingHistory() {
    // VEC legacy: /ui/billing/history — WEC (26.x SPA): /ui/#/billingHistory
    return (
      location.pathname.includes("billing/history") ||
      location.hash.includes("billing/history") ||
      location.hash.toLowerCase().includes("billinghistory")
    );
  }

  function isOnUsageExplorer() {
    return (
      location.pathname.includes("usageExplorer") ||
      location.hash.includes("usageExplorer")
    );
  }

  // ─── API-based capture (works from ANY signed-in page) ─────────────────
  // NISC SmartHub 26.x encodes session params as base64 in the SPA hash:
  //   #/home?<base64 of "includeInactive=false&custNbr=…&acctNbr=…&userId=…">
  // and exposes a cookie-authenticated JSON endpoint with the full billing
  // history INCLUDING kWh usage + meter-read period dates:
  //   GET /services/secured/billing/history/overview?acctNbr=NNN
  // This fires on the HOME page immediately after login — the operator never
  // has to click Billing History. The DOM scrape below remains as fallback
  // (and still runs on the billing-history page for older deployments).

  function decodeHashCreds() {
    const q = (location.hash.split("?")[1] || "").trim();
    if (!q) return null;
    try {
      const decoded = atob(q);
      if (!/acctNbr|custNbr|userId/.test(decoded)) return null;
      const p = new URLSearchParams(decoded);
      return {
        acctNbr: p.get("acctNbr"),
        custNbr: p.get("custNbr"),
        userId: p.get("userId"),
      };
    } catch (_) {
      return null;
    }
  }

  function acctsFromDom() {
    // Home page renders one heading per account:
    //   "982501 - 1519 WRIGHTS MTN ROAD, BRADFORD, VT 05033"
    const found = new Map();
    for (const h of document.querySelectorAll("h2, h3")) {
      const m = (h.textContent || "").trim().match(/^(\d{4,})\s*[-—–]\s*(.+)$/);
      if (m) found.set(m[1], m[2].trim());
    }
    return found;
  }

  function customerNameFromDom() {
    // Customer-overview card on #/home shows the account holder name
    // ("RICHARD G EVANS") in a .header-text span inside a mat-card.
    for (const el of document.querySelectorAll(".header-text, mat-card .header-text")) {
      const t = (el.textContent || "").trim();
      if (/^[A-Z][A-Z .'&-]{3,}$/.test(t) && !/OVERVIEW|NOTIFICATION|USAGE|PAYMENT|SMARTHUB/i.test(t)) {
        return t;
      }
    }
    return "";
  }

  function fmtDateMDY(ts) {
    // Guard against non-numeric / non-finite timestamps (null, "abc", Infinity)
    // that would otherwise yield "NaN/NaN/NaN" in bill records sent upstream.
    if (typeof ts !== "number" || !isFinite(ts)) return "";
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    return `${d.getMonth() + 1}/${d.getDate()}/${d.getFullYear()}`;
  }

  function isoDay(ts) {
    // Guard against null/NaN/non-finite timestamps that would otherwise throw
    // (RangeError on toISOString) or yield bogus dates in billing captures.
    // Returns null so period_start/period_end stay honestly empty rather than
    // shipping an invalid date upstream (mirrors fmtDateMDY's guard).
    const d = new Date(ts);
    if (isNaN(d.getTime())) return null;
    return d.toISOString().slice(0, 10);
  }

  let apiCaptureInFlight = false;
  let apiCaptureDone = false;

  async function tryApiCapture() {
    if (apiCaptureInFlight || apiCaptureDone) return apiCaptureDone;
    const creds = decodeHashCreds();
    const domAccts = acctsFromDom();
    const acctNbrs = new Set(domAccts.keys());
    if (creds && creds.acctNbr) acctNbrs.add(creds.acctNbr);
    if (acctNbrs.size === 0) return false;

    apiCaptureInFlight = true;
    try {
      const holderName = customerNameFromDom();
      const bills = [];
      for (const acctNbr of acctNbrs) {
        let rows;
        try {
          // Bound the request so a stalled fetch can't keep apiCaptureInFlight
          // latched forever (the page-session capture lock). 15s is generous for
          // a billing-history overview; AbortController guarantees the finally
          // below runs and the latch clears.
          const ctrl = new AbortController();
          const to = setTimeout(() => ctrl.abort(), 15000);
          try {
            const res = await fetch(
              `/services/secured/billing/history/overview?acctNbr=${encodeURIComponent(acctNbr)}`,
              { credentials: "include", signal: ctrl.signal }
            );
            if (!res.ok) continue;
            rows = await res.json();
          } finally {
            clearTimeout(to);
          }
        } catch (_) {
          continue;
        }
        for (const r of rows || []) {
          const loc = (r.servLocs && r.servLocs[0]) || {};
          const addr = loc.address || {};
          const addrStr =
            [addr.addr1, addr.city, addr.state, addr.zip].filter(Boolean).join(", ") ||
            domAccts.get(acctNbr) ||
            "";
          bills.push({
            account_id: String(r.acctNbr || acctNbr),
            customer_name: holderName,
            service_address: addrStr,
            billing_date: r.billingDateTimestamp ? fmtDateMDY(r.billingDateTimestamp) : "",
            bill_amount: r.adjustedBillAmount != null ? String(r.adjustedBillAmount) : "",
            adjustments: r.totalAdjustments != null ? String(r.totalAdjustments) : "",
            total_due: "",
            pdf_url: null,
            bill_uuid: r.billProcessUuid || null,
            bill_timestamp: r.billingDateTimestamp ? String(r.billingDateTimestamp) : null,
            // API-only riches: kWh + meter-read period (DOM scrape never had these)
            kwh: typeof r.totalUsage === "number" ? r.totalUsage : null,
            period_start: loc.lastBillPrevReadDtTm ? isoDay(loc.lastBillPrevReadDtTm) : null,
            period_end: loc.lastBillPresReadDtTm ? isoDay(loc.lastBillPresReadDtTm) : null,
            customer_number: r.custNbr ? String(r.custNbr) : (creds && creds.custNbr) || null,
            source: "api",
          });
        }
      }
      if (bills.length === 0) return false;
      if (creds && creds.userId && !capturedPrimaryUsername) {
        capturedPrimaryUsername = creds.userId;
      }
      await sendCapture(bills, [], "api");
      apiCaptureDone = true;
      return true;
    } finally {
      apiCaptureInFlight = false;
    }
  }

  // ─── Main capture loop ───────────────────────────────────────────────────

  async function tryScrape() {
    pollCount++;

    // Array-Operator meter-production capture: fire on every scrape pass (initial
    // load + each SPA nav). Self-guarded — only runs once, and only when an AO
    // vec/wec meter intent is armed. This does NOT depend on a fresh login firing
    // the auth-fetch hook (an already-signed-in owner never re-hits oauth/auth/v2),
    // which is why the v1.9.25 hook-only trigger silently never ran.
    maybeSendMeterCapture();

    let bills = [];
    let usage = [];

    if (isOnBillingHistory()) {
      bills = parseBillingHistory();
      if (bills.length === 0 && pollCount < MAX_POLLS) {
        setTimeout(tryScrape, POLL_INTERVAL_MS);
        return;
      }
    } else if (isOnUsageExplorer()) {
      usage = parseUsageExplorer();
      if (usage.length === 0 && pollCount < MAX_POLLS) {
        setTimeout(tryScrape, POLL_INTERVAL_MS);
        return;
      }
    } else {
      // Any other page (incl. #/home right after login): try the API capture.
      // Retry on the poll loop — the SPA hash creds / account headings may not
      // have rendered yet on the first ticks.
      const ok = await tryApiCapture();
      if (!ok && pollCount < MAX_POLLS) {
        setTimeout(tryScrape, POLL_INTERVAL_MS);
      }
      return;
    }

    if (bills.length === 0 && usage.length === 0) {
      // All three capture layers came up empty after MAX_POLLS — tell the
      // backend so deployments our parsers can't read show up on the drift
      // radar instead of failing silently. Best-effort, fires once per page.
      reportEmptyScrape();
      return;
    }
    await sendCapture(bills, usage, bills.length > 0 ? "dom" : "usage");
  }

  // ─── Empty-scrape telemetry ──────────────────────────────────────────────
  let emptyScrapeReported = false;
  function reportEmptyScrape() {
    if (emptyScrapeReported) return;
    if (!(isOnBillingHistory() || isOnUsageExplorer())) return;
    emptyScrapeReported = true;
    chrome.runtime.sendMessage(
      {
        type: "SMARTHUB_SCRAPE_EMPTY",
        provider: PROVIDER,
        hostname: location.hostname,
        page: location.pathname + location.hash,
        extensionVersion: chrome.runtime.getManifest().version,
        at: new Date().toISOString(),
      },
      () => { void chrome.runtime.lastError; }
    );
  }

  async function sendCapture(bills, usage, method) {
    const accounts = extractAccounts(bills);
    const authBlock = capturedAuthToken
      ? {
          apiToken: capturedAuthToken,
          username: capturedPrimaryUsername || undefined,
        }
      : {};

    const payload = {
      provider: PROVIDER,
      captureMethod: method || "dom",
      extensionVersion: chrome.runtime.getManifest().version,
      capturedAt: new Date().toISOString(),
      pageUrl: location.href,
      user: {
        hostname: location.hostname,
        utility: UTILITY_NAME,
        // Include email/username if the auth intercept ran before scraping
        ...(capturedPrimaryUsername ? { username: capturedPrimaryUsername } : {}),
      },
      auth: authBlock,
      accounts,
      bills,
      usage,
    };

    // Dedupe: skip if this exact capture was already sent
    const fingerprint = JSON.stringify({
      bills: bills.length,
      usage: usage.length,
      acct: accounts[0]?.accountNumber,
      page: location.pathname + location.hash,
      provider: PROVIDER,
    });
    const hash = await hashString(fingerprint);
    if (hash === lastSentHash) return;
    lastSentHash = hash;

    chrome.runtime.sendMessage(
      { type: "SMARTHUB_DATA_CAPTURED", payload, tokenHash: hash },
      (response) => {
        if (chrome.runtime.lastError) {
          console.warn(
            `[EnergyAgent ${UTILITY_NAME}] sendMessage failed:`,
            chrome.runtime.lastError
          );
          return;
        }
        if (response && response.ok) {
          console.log(
            `[EnergyAgent ${UTILITY_NAME}] Synced: ` +
              `${accounts.length} account(s), ${bills.length} bill row(s), ` +
              `${usage.length} usage row(s) → ${response.endpoint}`
          );
        } else if (response && response.error) {
          console.warn(`[EnergyAgent ${UTILITY_NAME}] Sync error:`, response.error);
        }
      }
    );
  }

  // Re-scrape when the Angular SPA navigates (hash or path changes)
  let lastUrl = location.href;
  setInterval(() => {
    const current = location.href;
    if (current !== lastUrl) {
      lastUrl = current;
      pollCount = 0;
      // Clear the once-per-page API-capture latch so navigating to a fresh page
      // (e.g. an operator switching to a different customer's account in the SPA)
      // can run a new capture. The sendCapture() fingerprint dedup still prevents
      // re-sending an identical payload. Also clear apiCaptureInFlight: if a prior
      // capture's fetch stalled, its finally never ran, so the in-flight latch
      // would otherwise lock out every future capture for the page session.
      apiCaptureDone = false;
      apiCaptureInFlight = false;
      tryScrape();
      broadcastLoginState();
    }
  }, 2000);

  // ── Login-state detection ─────────────────────────────────────────────────
  // SmartHub uses cookie auth + Angular SPA. Detect and broadcast login state
  // so the onboarding wizard can mirror it live.

  let lastLoginState = null;

  function detectLoginState() {
    const url = location.href.toLowerCase();
    if (url.includes("/services/secured/") || url.includes("/dashboard")) {
      return "signed_in";
    }
    if (url.includes("/login") || url.includes("signin") || url.includes("sign-in")) {
      return "login_required";
    }
    if (document.querySelector('input[type="password"]')) {
      return "login_required";
    }
    if (
      document.querySelector("table") &&
      /usage|billing|history/i.test(document.body.innerText || "")
    ) {
      return "signed_in";
    }
    return "unknown";
  }

  function broadcastLoginState() {
    const state = detectLoginState();
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage(
      {
        type: "LOGIN_STATE_DETECTED",
        provider: PROVIDER,
        state,
        url: location.href,
        at: new Date().toISOString(),
      },
      () => { void chrome.runtime.lastError; }
    );
  }

  setInterval(broadcastLoginState, 2500);
  broadcastLoginState();

  tryScrape();
})();
