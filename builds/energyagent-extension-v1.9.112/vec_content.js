// vec_content.js — runs on NISC SmartHub pages (Vermont Electric Cooperative,
// Washington Electric Co-op, Stowe Electric, and other *.smarthub.coop hosts).
//
// Auth model: NISC SmartHub uses cookie-based sessions. There is no localStorage
// token to capture — we scrape already-rendered DOM instead.
//
// Two pages we know how to scrape:
//   /ui/billing/history     — Angular table: bill rows with dates, amounts, PDF links
//   /ui/#/usageExplorer     — SVG chart: kWh exposed as aria-labels on <image> elements
//
// Routing note: Chrome's content_scripts manifest entry routes by hostname.
// This file handles ALL *.smarthub.coop subdomains. VEC (vermontelectric) is the
// only tested subdomain — others get a runtime warning.

(function () {
  "use strict";

  const PROVIDER = "vec";
  const POLL_INTERVAL_MS = 2000;
  const MAX_POLLS = 30; // 60 seconds before giving up

  const KNOWN_HOST = "vermontelectric.smarthub.coop";
  if (location.hostname !== KNOWN_HOST) {
    console.warn(
      `[EnergyAgent] Untested SmartHub host: ${location.hostname}. ` +
        `Treating as NISC SmartHub (same as ${KNOWN_HOST}). Data shape may differ.`
    );
  }

  let pollCount = 0;
  let lastSentHash = null;

  async function hashString(s) {
    const buf = new TextEncoder().encode(s);
    const digest = await crypto.subtle.digest("SHA-1", buf);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }

  // ─── billing history scraper ─────────────────────────────────────────────
  // Columns (0-indexed): Account# | AutoPay | CustomerName | Address |
  //   BillingDate | BillAmount | Adjustments | TotalDue | [ViewBill link]
  // Date cell (index 4) format: MM/DD/YYYY — used as the anchor to detect data rows.

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

  // ─── usage explorer scraper ──────────────────────────────────────────────
  // aria-label format (NISC SmartHub template):
  //   "Jun 2023 Billing Period. Usage Dates: May 18 - June 17.
  //    Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F"

  const ARIA_RE =
    /^([^\n.]+?)\s+Billing Period\.\s+Usage Dates:\s+([^\n.]+?)\s*\.[\s\S]+?Meter\s+(\d+)\s+-\s+[^\n\-]+?\s+-\s+kWh:\s+([\d.]+)\s+kWh(?:[\s\S]*?Average Temperature:\s+([\d.]+)\s*°?F)?/i;

  function parseUsageExplorer() {
    const rows = [];
    const images = document.querySelectorAll(
      "image[aria-label], svg image[aria-label]"
    );
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

  // ─── account list from billing rows ─────────────────────────────────────

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

  // ─── page detection ──────────────────────────────────────────────────────

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

  // ─── main capture loop ───────────────────────────────────────────────────

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
    const payload = {
      provider: PROVIDER,
      capturedAt: new Date().toISOString(),
      pageUrl: location.href,
      user: { hostname: location.hostname },
      auth: {}, // cookie-based auth — no capturable token
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
    });
    const hash = await hashString(fingerprint);
    if (hash === lastSentHash) return;
    lastSentHash = hash;

    chrome.runtime.sendMessage(
      { type: "VEC_DATA_CAPTURED", payload, tokenHash: hash },
      (response) => {
        if (chrome.runtime.lastError) {
          console.warn(
            "[EnergyAgent VEC] sendMessage failed:",
            chrome.runtime.lastError
          );
          return;
        }
        if (response && response.ok) {
          console.log(
            `[EnergyAgent VEC] Synced: ${accounts.length} account(s), ` +
              `${bills.length} bill row(s), ${usage.length} usage row(s) → ${response.endpoint}`
          );
        } else if (response && response.error) {
          console.warn("[EnergyAgent VEC] Sync error:", response.error);
        }
      }
    );
  }

  // Re-scrape when the Angular SPA navigates (hash or path changes)
  let lastUrl = location.href;
  setInterval(() => {
    const now = location.href;
    if (now !== lastUrl) {
      lastUrl = now;
      pollCount = 0;
      tryScrape();
      broadcastLoginState();
    }
  }, 2000);

  // ── Login-state detection (v1.3.0) ─────────────────────────────────────
  // SmartHub uses cookie auth + an Angular SPA. We detect:
  //   - login_required: URL contains /login OR a password input is on the page
  //   - signed_in:      we successfully scraped any billing/usage data so far,
  //                     OR we're on a /services/secured/ URL path
  //   - unknown:        page is mid-load
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
    // If we have either bill or usage rows in the DOM we're signed in.
    if (document.querySelector("table") && /usage|billing|history/i.test(document.body.innerText || "")) {
      return "signed_in";
    }
    return "unknown";
  }
  function broadcastLoginState() {
    const state = detectLoginState();
    if (state === lastLoginState) return;
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED",
      provider: "vec",
      state,
      url: location.href,
      at: new Date().toISOString(),
    }, () => { void chrome.runtime.lastError; });
  }
  setInterval(broadcastLoginState, 2500);
  broadcastLoginState();

  tryScrape();
})();
