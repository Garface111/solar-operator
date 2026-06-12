(async function () {
  const PROD_ENDPOINT = "https://api.solaroperator.org/v1/sync";

  const endpointEl = document.getElementById("endpoint");
  const tenantEl = document.getElementById("tenant_key");
  const savedMsg = document.getElementById("saved-msg");
  const returnSetup = document.getElementById("return-setup");
  const lastCaptureEl = document.getElementById("last-capture");
  const acctCountEl = document.getElementById("acct-count");
  const tokenExpiresEl = document.getElementById("token-expires");

  const s = await chrome.storage.local.get([
    "api_endpoint", "tenant_key", "last_payload", "last_sync", "last_error",
  ]);

  endpointEl.value = s.api_endpoint || PROD_ENDPOINT;
  tenantEl.value = s.tenant_key || "";

  // Friendly status rendering
  if (s.last_payload) {
    const capturedAt = s.last_payload.capturedAt
      ? new Date(s.last_payload.capturedAt).toLocaleString()
      : "—";
    lastCaptureEl.textContent = capturedAt;
    acctCountEl.textContent = s.last_payload.accountCount ?? "—";
    if (s.last_payload.tokenExpires) {
      const expDate = new Date(s.last_payload.tokenExpires);
      const daysLeft = Math.round(
        (expDate.getTime() - Date.now()) / (1000 * 60 * 60 * 24)
      );
      tokenExpiresEl.textContent =
        daysLeft > 0 ? `${expDate.toLocaleDateString()} (${daysLeft}d)` : "expired";
    }
  }

  document.getElementById("save").addEventListener("click", async () => {
    const key = tenantEl.value.trim();
    const endpoint = endpointEl.value.trim() || PROD_ENDPOINT;
    await chrome.storage.local.set({
      tenant_key: key,
      api_endpoint: endpoint,
    });
    savedMsg.style.display = "block";
    setTimeout(() => (savedMsg.style.display = "none"), 2200);
    // Direct the user back to onboarding to finish setup. Persist this link
    // (don't auto-hide) so they always have a clear way back.
    returnSetup.style.display = "block";
  });

  document.getElementById("clear").addEventListener("click", async () => {
    if (!confirm("Disconnect this device? You'll need to visit greenmountainpower.com again to re-capture your session.")) return;
    await chrome.storage.local.remove([
      "last_payload", "last_sync", "last_error",
    ]);
    lastCaptureEl.textContent = "—";
    acctCountEl.textContent = "—";
    tokenExpiresEl.textContent = "—";
  });
})();
