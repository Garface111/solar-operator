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

  const POLL_INTERVAL_MS = 2000;
  const MAX_POLLS = 30; // 60 seconds before giving up

  let pollCount = 0;
  let lastSentHash = null;

  // ─── Auth token interception ─────────────────────────────────────────────
  // Monkey-patch fetch to intercept the SmartHub login API response and
  // extract the authorizationToken. This enables server-side generation pulls
  // without requiring the operator to enter their password in Solar Operator.
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
            }
          }).catch(() => {});
        } catch (_) {}
      }
      return resp;
    };
  })();

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
      if (cells.length < 8) continue;
      const maybeDate = (cells[4]?.textContent || "").trim();
      if (!/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(maybeDate)) continue;

      const link = tr.querySelector("a[href*='billPdfService']");
      let pdfUrl = link ? link.href : null;
      let billUuid = null;
      let billTimestamp = null;
      let accountId = (cells[0]?.textContent || "").trim();

      if (pdfUrl) {
        try {
          const u = new URL(pdfUrl);
          billUuid = u.searchParams.get("uuid");
          billTimestamp = u.searchParams.get("timestamp");
          if (!accountId) accountId = u.searchParams.get("account") || "";
        } catch (_) {}
      }

      rows.push({
        account_id: accountId,
        customer_name: (cells[2]?.textContent || "").trim(),
        service_address: (cells[3]?.textContent || "").trim(),
        billing_date: maybeDate,
        bill_amount: (cells[5]?.textContent || "").replace(/[^0-9.\-]/g, ""),
        adjustments: (cells[6]?.textContent || "").replace(/[^0-9.\-]/g, ""),
        total_due: (cells[7]?.textContent || "").replace(/[^0-9.\-]/g, ""),
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

  const ARIA_RE =
    /^([^\n.]+?)\s+Billing Period\.\s+Usage Dates:\s+([^\n.]+?)\s*\.[\s\S]+?Meter\s+(\d+)\s+-\s+[^\n\-]+?\s+-\s+kWh:\s+([\d.]+)\s+kWh(?:[\s\S]*?Average Temperature:\s+([\d.]+)\s*°?F)?/i;

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
    return (
      location.pathname.includes("billing/history") ||
      location.hash.includes("billing/history")
    );
  }

  function isOnUsageExplorer() {
    return (
      location.pathname.includes("usageExplorer") ||
      location.hash.includes("usageExplorer")
    );
  }

  // ─── Main capture loop ───────────────────────────────────────────────────

  async function tryScrape() {
    pollCount++;

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
      return; // not a page we handle
    }

    if (bills.length === 0 && usage.length === 0) return;

    const accounts = extractAccounts(bills);
    const authBlock = capturedAuthToken
      ? {
          apiToken: capturedAuthToken,
          username: capturedPrimaryUsername || undefined,
        }
      : {};

    const payload = {
      provider: PROVIDER,
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
            `[Solar Operator ${UTILITY_NAME}] sendMessage failed:`,
            chrome.runtime.lastError
          );
          return;
        }
        if (response && response.ok) {
          console.log(
            `[Solar Operator ${UTILITY_NAME}] Synced: ` +
              `${accounts.length} account(s), ${bills.length} bill row(s), ` +
              `${usage.length} usage row(s) → ${response.endpoint}`
          );
        } else if (response && response.error) {
          console.warn(`[Solar Operator ${UTILITY_NAME}] Sync error:`, response.error);
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
