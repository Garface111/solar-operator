(async function () {
  "use strict";

  const pillEl        = document.getElementById("status-pill");
  const toastEl       = document.getElementById("toast");
  const errorBlockEl  = document.getElementById("error-block");
  const errorMsgEl    = document.getElementById("error-msg");
  const retryBtnEl    = document.getElementById("retry-btn");
  const lastCaptureEl = document.getElementById("last-capture");
  const countTodayEl  = document.getElementById("count-today");

  // ── Load state from storage ───────────────────────────────────────────────
  const s = await chrome.storage.local.get([
    "tenant_key", "last_sync", "last_payload", "last_error", "captures_today",
  ]);

  const hasKey = !!s.tenant_key;
  const ls = s.last_sync    || null;
  const lp = s.last_payload || null;
  const le = s.last_error   || null;
  const ct = s.captures_today || null;

  // Error is "recent" if it happened within 5 min and after the last good sync.
  const ERROR_WINDOW_MS = 5 * 60 * 1000;
  const isRecentError = le &&
    (Date.now() - new Date(le.at).getTime()) < ERROR_WINDOW_MS &&
    (!ls || new Date(le.at) > new Date(ls.at));

  // ── Status pill ───────────────────────────────────────────────────────────
  if (!hasKey) {
    setPill("Not paired", "pill-not-paired");
  } else if (isRecentError) {
    setPill("API offline", "pill-offline");
  } else {
    setPill("Connected to EnergyAgent", "pill-connected");
  }

  function setPill(text, cls) {
    pillEl.textContent = text;
    pillEl.className = `pill ${cls}`;
  }

  // ── Error block ───────────────────────────────────────────────────────────
  if (isRecentError) {
    errorMsgEl.textContent = le.message;
    errorBlockEl.classList.remove("hidden");
  }

  // ── Utility-aware portal link ─────────────────────────────────────────────
  // The footer link + retry button open the LAST-CAPTURED utility's portal,
  // not hardcoded GMP — a WEC/VEC operator should bounce back to SmartHub.
  // smarthub_registry.js (loaded before this script) provides the host map.
  const lastProvider = (lp && lp.provider) || "gmp";
  let portalUrl = "https://greenmountainpower.com/";
  let portalName = "GMP";
  if (lastProvider !== "gmp" && window.SMARTHUB_REGISTRY) {
    for (const [host, entry] of Object.entries(window.SMARTHUB_REGISTRY)) {
      if (entry.provider === lastProvider) {
        portalUrl = `https://${host}/`;
        // Short label: first word of the utility name, or the code uppercased
        portalName = (entry.name || lastProvider).split(" ")[0];
        if (portalName.length <= 4) portalName = lastProvider.toUpperCase();
        break;
      }
    }
  }

  // Retry: open the operator's utility portal to trigger a fresh capture.
  retryBtnEl.addEventListener("click", () => {
    chrome.tabs.create({ url: portalUrl });
    window.close();
  });

  // ── Last capture ──────────────────────────────────────────────────────────
  if (lp && lp.capturedAt) {
    const provider = (lp.provider || "gmp").toUpperCase();
    lastCaptureEl.textContent = `${provider} · ${timeAgo(lp.capturedAt)}`;
  }

  // ── Count today ───────────────────────────────────────────────────────────
  const todayStr = new Date().toISOString().slice(0, 10);
  if (ct && ct.date === todayStr && ct.count > 0) {
    countTodayEl.textContent = `${ct.count} capture${ct.count === 1 ? "" : "s"} today`;
  } else {
    countTodayEl.textContent = "0 captures today";
  }

  // ── Buttons ───────────────────────────────────────────────────────────────
  document.getElementById("open-dashboard").addEventListener("click", () => {
    // Dashboard SPA lives at solaroperator.org/accounts (Netlify 200-proxy to
    // Railway /app/ — see solaroperator-site/_redirects). Plain /app 404s.
    chrome.tabs.create({ url: "https://solaroperator.org/accounts" });
  });

  document.getElementById("open-options").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  const openPortalEl = document.getElementById("open-gmp");
  openPortalEl.textContent = `Open ${portalName}`;
  openPortalEl.addEventListener("click", (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: portalUrl });
  });

  // ── Live toast on SO_CAPTURE_LANDED (while popup is open) ─────────────────
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type !== "SO_CAPTURE_LANDED") return;
    showToast(msg.ok);
  });

  function showToast(ok) {
    toastEl.textContent = ok ? "✓ Captured!" : "⚠ Capture failed";
    toastEl.classList.remove("hidden", "fading");
    setTimeout(() => {
      toastEl.classList.add("fading");
      setTimeout(() => toastEl.classList.add("hidden"), 400);
    }, 3000);
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function timeAgo(iso) {
    const ms = Date.now() - new Date(iso).getTime();
    const m = Math.round(ms / 60000);
    if (m < 1)  return "just now";
    if (m < 60) return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h} hr${h === 1 ? "" : "s"} ago`;
    const d = Math.round(h / 24);
    return `${d} day${d === 1 ? "" : "s"} ago`;
  }

  // ── Auto-login section ────────────────────────────────────────────────────
  const AL_VENDORS = [
    { id: "fronius", label: "Fronius (Solar.web)" },
    { id: "sma", label: "SMA (Sunny Portal)" },
    { id: "chint", label: "Chint" },
  ];
  const alToggle = document.getElementById("al-toggle");
  const alBody = document.getElementById("al-body");
  const alChev = document.getElementById("al-chev");
  const alRows = document.getElementById("al-rows");
  const alTpl = document.getElementById("al-row-tpl");

  function vaultMsg(payload) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(payload, (resp) => {
        void chrome.runtime.lastError; resolve(resp || { ok: false });
      });
    });
  }

  if (alToggle) {
    alToggle.addEventListener("click", () => {
      const open = alBody.classList.toggle("hidden") === false;
      alChev.textContent = open ? "▾" : "▸";
      if (open) renderAutoLogin();
    });
  }

  async function renderAutoLogin() {
    const resp = await vaultMsg({ type: "SO_VAULT_STATUS" });
    const status = (resp && resp.status) || {};
    alRows.innerHTML = "";
    for (const v of AL_VENDORS) {
      const st = status[v.id] || { hasCreds: false, enabled: true };
      const node = alTpl.content.cloneNode(true);
      const row = node.querySelector(".al-row");
      row.querySelector(".al-vendor").textContent = v.label;
      const stateEl = row.querySelector(".al-state");
      stateEl.textContent = st.hasCreds ? (st.enabled ? "● saved · on" : "● saved · off") : "not set";
      stateEl.className = "al-state " + (st.hasCreds && st.enabled ? "on" : st.hasCreds ? "off" : "");
      const userEl = row.querySelector(".al-user");
      const passEl = row.querySelector(".al-pass");
      const saveBtn = row.querySelector(".al-save");
      const clearBtn = row.querySelector(".al-clear");
      const optCb = row.querySelector(".al-optout-cb");
      if (st.hasCreds) { passEl.placeholder = "•••••••• (saved — type to replace)"; clearBtn.classList.remove("hidden"); }
      optCb.checked = !st.enabled;   // checkbox = "off" (opted out)
      saveBtn.addEventListener("click", async () => {
        const u = userEl.value.trim(); const p = passEl.value;
        if (!u || !p) { saveBtn.textContent = "enter both"; setTimeout(() => saveBtn.textContent = "Save", 1500); return; }
        saveBtn.textContent = "Saving…";
        const r = await vaultMsg({ type: "SO_VAULT_SET", vendor: v.id, username: u, password: p });
        saveBtn.textContent = r.ok ? "✓ Saved" : "failed";
        passEl.value = "";
        setTimeout(renderAutoLogin, 900);
      });
      clearBtn.addEventListener("click", async () => {
        await vaultMsg({ type: "SO_VAULT_CLEAR", vendor: v.id });
        renderAutoLogin();
      });
      optCb.addEventListener("change", async () => {
        await vaultMsg({ type: "SO_VAULT_OPTOUT", vendor: v.id, optedOut: optCb.checked });
        renderAutoLogin();
      });
      alRows.appendChild(node);
    }
  }
})();
