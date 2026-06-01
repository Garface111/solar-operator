(async function () {
  const statusEl = document.getElementById("status");
  const detailEl = document.getElementById("detail");

  const s = await chrome.storage.local.get([
    "last_payload", "last_sync", "last_error", "tenant_key",
  ]);

  const lp = s.last_payload;
  const ls = s.last_sync;
  const le = s.last_error;
  const hasKey = !!s.tenant_key;

  // Status banner logic — clearer for non-technical users
  if (!hasKey) {
    statusEl.className = "status warn";
    statusEl.innerHTML = `<strong>Activation code needed.</strong> Click Settings below and paste your code from the welcome email.`;
  } else if (le && (!ls || new Date(le.at) > new Date(ls.at))) {
    statusEl.className = "status warn";
    statusEl.innerHTML = `<strong>Connection problem.</strong> ${escapeHtml(le.message)}`;
  } else if (ls && lp) {
    statusEl.className = "status ok";
    const when = timeAgo(ls.at);
    statusEl.innerHTML = `<strong>✓ Connected.</strong> Last sync ${when}.`;
  } else {
    statusEl.className = "status idle";
    statusEl.innerHTML = `<strong>Ready.</strong> Visit <a href="#" id="goto-gmp">greenmountainpower.com</a> and sign in to start.`;
    const goto = document.getElementById("goto-gmp");
    if (goto) goto.addEventListener("click", openGmp);
  }

  if (lp) {
    const expiresAt = lp.tokenExpires ? new Date(lp.tokenExpires) : null;
    const daysLeft = expiresAt
      ? Math.max(0, Math.ceil((expiresAt - Date.now()) / 86400000))
      : null;
    const daysClass = daysLeft != null && daysLeft < 3 ? "warn-text" : "";
    const daysLabel = daysLeft != null
      ? (daysLeft === 0 ? "expired" : `${daysLeft} day${daysLeft === 1 ? "" : "s"}`)
      : "—";

    detailEl.innerHTML = `
      <div class="row"><span class="k">Arrays</span><span class="v">${lp.accountCount ?? "—"}</span></div>
      <div class="row"><span class="k">Session refresh</span><span class="v ${daysClass}">${daysLabel}</span></div>
    `;

    if (daysLeft != null && daysLeft < 5) {
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.innerHTML = `Visit greenmountainpower.com soon to keep your session active.`;
      detailEl.appendChild(hint);
    }
  }

  document.getElementById("open-options").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });
  document.getElementById("open-gmp").addEventListener("click", openGmp);

  function openGmp(e) {
    if (e) e.preventDefault();
    chrome.tabs.create({ url: "https://greenmountainpower.com/" });
  }

  function timeAgo(iso) {
    const ms = Date.now() - new Date(iso).getTime();
    const m = Math.round(ms / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h} hour${h === 1 ? "" : "s"} ago`;
    const d = Math.round(h / 24);
    return `${d} day${d === 1 ? "" : "s"} ago`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      "\"": "&quot;", "'": "&#39;",
    }[c]));
  }
})();
