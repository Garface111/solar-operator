// content.js — runs on greenmountainpower.com pages
// Reads the gmp-vue localStorage payload (where the JWT + account map live),
// extracts what EnergyAgent needs, and hands it to the service worker.
//
// IMPORTANT: this script runs in an isolated world by default, which means
// it CAN see the DOM but CANNOT see the page's window.* JavaScript variables.
// localStorage IS accessible from the isolated world, so we're fine reading it.

(function () {
  "use strict";

  const GMP_KEY = "gmp-vue";
  const POLL_INTERVAL_MS = 5000;
  const MAX_POLLS = 12; // 60 seconds total — give the SPA time to log in

  let pollCount = 0;
  let lastSentTokenHash = null;
  let lastLoginState = null;  // dedupe SO_LOGIN_STATE broadcasts

  // ── Login-state detection (v1.3.0) ─────────────────────────────────────
  // The SPA's onboarding screen mirrors what's happening in the utility tab
  // (background tab the user can't see). We classify the current page as
  // login_required / signed_in / unknown and notify background.js, which
  // forwards to every solaroperator.org tab via SO_LOGIN_STATE.
  //
  // GMP heuristics (all DOM-based — no API calls):
  //   - signed_in   = gmp-vue localStorage has a valid user.apitoken
  //   - login_required = URL contains /login or there's an obvious
  //     <input type="password"> + sign-in CTA on the page
  //   - unknown     = neither — page is mid-load or some other GMP page
  function detectLoginState() {
    try {
      const raw = localStorage.getItem(GMP_KEY);
      if (raw) {
        const outer = JSON.parse(raw);
        if (outer && outer.user && outer.user.apitoken) return "signed_in";
      }
    } catch { /* fall through */ }
    const url = location.href.toLowerCase();
    if (url.includes("/login") || url.includes("signin") || url.includes("sign-in")) {
      return "login_required";
    }
    // Heuristic: a password field + a "sign in"/"log in" button text on the page.
    if (document.querySelector('input[type="password"]')) {
      const text = document.body ? document.body.innerText.toLowerCase() : "";
      if (/\bsign in\b|\blog in\b|\bsign-in\b|\blog-in\b/.test(text)) {
        return "login_required";
      }
    }
    return "unknown";
  }

  function broadcastLoginState() {
    const state = detectLoginState();
    if (state === lastLoginState) return; // dedupe
    lastLoginState = state;
    chrome.runtime.sendMessage({
      type: "LOGIN_STATE_DETECTED",
      provider: "gmp",
      state,
      url: location.href,
      at: new Date().toISOString(),
    }, () => {
      // Swallow lastError — broadcasting is best-effort.
      void chrome.runtime.lastError;
    });
  }

  // Light SHA-1 so we don't re-POST identical payloads.
  async function hashString(s) {
    const buf = new TextEncoder().encode(s);
    const digest = await crypto.subtle.digest("SHA-1", buf);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }

  function readGmpPayload() {
    try {
      const raw = localStorage.getItem(GMP_KEY);
      if (!raw) return null;
      const outer = JSON.parse(raw);
      // The Vue store wraps everything under .user
      if (!outer || !outer.user || !outer.user.apitoken) return null;
      return outer;
    } catch (e) {
      console.warn("[EnergyAgent] Failed to parse gmp-vue:", e);
      return null;
    }
  }

  function extractAccounts(payload) {
    // accounts array is at outer.user.accounts (post-login, with full bill metadata)
    // Fall back to outer.user.userinfo.customData.energyAccounts (minimal — just names + numbers)
    const u = payload.user;
    if (Array.isArray(u.accounts) && u.accounts.length > 0) {
      return u.accounts.map((a) => ({
        accountNumber: a.accountNumber,
        nickname: a.nickname,
        customerNumber: a.personId,
        currentBillUrl: a.currentBillUrl,
        currentBillUrlBinary: a.currentBillUrlBinary,
        serviceAddress: a.address,
        solarNetMeter: a.solarNetMeter,
        groupNetMetered: a.groupNetMetered,
        isPrimary: a.isPrimary,
      }));
    }
    const ea = u.userinfo?.customData?.energyAccounts || [];
    return ea.map((a) => ({
      accountNumber: a.accountNumber,
      nickname: a.nickname,
      currentBillUrl: null,
    }));
  }

  function buildSyncPayload(payload) {
    const u = payload.user;
    const ui = u.userinfo || {};
    return {
      provider: "gmp",
      capturedAt: new Date().toISOString(),
      pageUrl: location.href,
      user: {
        accountId: ui.accountId,
        username: ui.username,
        email: ui.email,
        fullName: ui.fullName,
      },
      auth: {
        apiToken: u.apitoken,
        apiTokenExpires: u.apitokenExpires,
        refreshToken: u.refreshtoken,
      },
      accounts: extractAccounts(payload),
    };
  }

  async function tryCapture() {
    pollCount++;
    const payload = readGmpPayload();
    if (!payload) {
      if (pollCount < MAX_POLLS) {
        setTimeout(tryCapture, POLL_INTERVAL_MS);
      }
      return;
    }

    const sync = buildSyncPayload(payload);
    const tokenHash = await hashString(sync.auth.apiToken);
    if (tokenHash === lastSentTokenHash) return; // dedupe
    lastSentTokenHash = tokenHash;

    chrome.runtime.sendMessage(
      { type: "GMP_TOKEN_CAPTURED", payload: sync, tokenHash },
      (response) => {
        if (chrome.runtime.lastError) {
          console.warn("[EnergyAgent] sendMessage failed:", chrome.runtime.lastError);
          return;
        }
        if (response && response.ok) {
          console.log(`[EnergyAgent] Synced ${sync.accounts.length} accounts to ${response.endpoint}`);
        } else if (response && response.error) {
          console.warn("[EnergyAgent] Sync error:", response.error);
        }
      }
    );
  }

  // Re-capture when SPA navigates between pages (the JWT can refresh)
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      pollCount = 0;
      tryCapture();
      broadcastLoginState();
    }
  }, 2000);

  // Periodic login-state probe — catches the transition from login form to
  // signed-in app even when the URL doesn't change (GMP's Vue router).
  setInterval(broadcastLoginState, 2500);

  // Initial sweep
  broadcastLoginState();
  tryCapture();
})();
