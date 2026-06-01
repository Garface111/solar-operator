// content.js — runs on greenmountainpower.com pages
// Reads the gmp-vue localStorage payload (where the JWT + account map live),
// extracts what Solar Operator needs, and hands it to the service worker.
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
      console.warn("[Solar Operator] Failed to parse gmp-vue:", e);
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
          console.warn("[Solar Operator] sendMessage failed:", chrome.runtime.lastError);
          return;
        }
        if (response && response.ok) {
          console.log(`[Solar Operator] Synced ${sync.accounts.length} accounts to ${response.endpoint}`);
        } else if (response && response.error) {
          console.warn("[Solar Operator] Sync error:", response.error);
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
    }
  }, 2000);

  // Initial sweep
  tryCapture();
})();
