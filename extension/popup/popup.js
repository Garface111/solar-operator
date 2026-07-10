(async function () {
  "use strict";

  const pillEl        = document.getElementById("status-pill");
  const pillTextEl    = pillEl.querySelector(".pill-text");
  const toastEl       = document.getElementById("toast");
  const errorBlockEl  = document.getElementById("error-block");
  const errorMsgEl    = document.getElementById("error-msg");
  const retryBtnEl    = document.getElementById("retry-btn");
  const lastCaptureEl = document.getElementById("last-capture");
  const countTodayEl  = document.getElementById("count-today");
  const statBlockEl   = document.getElementById("stat-block");
  const statCellTpl   = document.getElementById("stat-cell-tpl");
  const badgeAoEl     = document.getElementById("badge-ao");
  const badgeNepoolEl = document.getElementById("badge-nepool");
  const dashBtnEl     = document.getElementById("open-dashboard");
  const secondaryBtnEl= document.getElementById("open-secondary");

  // Product dashboard hosts.
  const AO_DASHBOARD     = "https://arrayoperator.com";
  const NEPOOL_DASHBOARD = "https://solaroperator.org/accounts";
  const DEFAULT_API_BASE = "https://nepooloperator.com";

  // ── Load state from storage ───────────────────────────────────────────────
  const s = await chrome.storage.local.get([
    "tenant_key", "api_endpoint", "last_sync", "last_payload",
    "last_error", "captures_today",
  ]);

  const hasKey = !!s.tenant_key;
  const ls = s.last_sync    || null;
  const lp = s.last_payload || null;
  const le = s.last_error   || null;
  const ct = s.captures_today || null;

  // API base for the status fetch: derive from the configured sync endpoint
  // (same default + override the background uses), stripping the /v1/sync tail.
  const apiBase = ((s.api_endpoint || "").replace(/\/v1\/sync\/?$/, "")
                   || DEFAULT_API_BASE).replace(/\/$/, "");

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
    setPill("Connected", "pill-connected");
  }

  function setPill(text, cls) {
    pillTextEl.textContent = text;
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
  const lastProvider = (lp && lp.provider) || "gmp";
  let portalUrl = "https://greenmountainpower.com/";
  let portalName = "GMP";
  if (lastProvider !== "gmp" && window.SMARTHUB_REGISTRY) {
    for (const [host, entry] of Object.entries(window.SMARTHUB_REGISTRY)) {
      if (entry.provider === lastProvider) {
        portalUrl = `https://${host}/`;
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
    countTodayEl.textContent = `${ct.count} capture${ct.count === 1 ? "" : "s"}`;
  } else {
    countTodayEl.textContent = "0 captures";
  }

  // ── Default dashboard button (NEPOOL host) — overridden once product known ─
  let dashUrl = NEPOOL_DASHBOARD;
  dashBtnEl.addEventListener("click", () => chrome.tabs.create({ url: dashUrl }));
  secondaryBtnEl.addEventListener("click", () => {
    const u = secondaryBtnEl.dataset.url;
    if (u) chrome.tabs.create({ url: u });
  });

  // ── Settings + utility-portal footer links ────────────────────────────────
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

  // ── Product + live stats (from the backend) ───────────────────────────────
  // One tenant_key → one Tenant → one product, so today exactly one badge lights.
  // The dim badge stays visible so a future multi-product install can light it.
  if (hasKey) {
    fetchStatus(s.tenant_key).then(applyStatus).catch(() => {
      // Offline / unauthorized: leave the storage-derived UI as-is. The badges
      // stay dim and the dashboard button keeps its NEPOOL default.
    });
  }

  async function fetchStatus(key) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 6000);
    try {
      const r = await fetch(`${apiBase}/v1/array-owners/extension-status`, {
        headers: { Authorization: `Bearer ${key}` },
        signal: ctrl.signal,
      });
      if (!r.ok) throw new Error(`status ${r.status}`);
      return await r.json();
    } finally {
      clearTimeout(t);
    }
  }

  function applyStatus(data) {
    if (!data || !data.product) return;
    const product = data.product;

    // Light the linked badge.
    if (product === "array_operator") {
      badgeAoEl.classList.remove("is-dim"); badgeAoEl.classList.add("is-lit");
    } else {
      badgeNepoolEl.classList.remove("is-dim"); badgeNepoolEl.classList.add("is-lit");
    }

    // Product-correct dashboard button + a secondary portal shortcut.
    if (product === "array_operator") {
      dashUrl = AO_DASHBOARD;
      dashBtnEl.textContent = "Open Array Operator";
      dashBtnEl.classList.add("ao");
    } else {
      dashUrl = NEPOOL_DASHBOARD;
      dashBtnEl.textContent = "Open NEPOOL Operator";
      dashBtnEl.classList.add("nepool");
    }

    // Dense stat block.
    renderStats(product, data);

    // Prefer the backend's last_capture (covers inverter-only AO installs that
    // have no last_payload yet) when storage has nothing fresher.
    if (data.last_capture && data.last_capture.at && !(lp && lp.capturedAt)) {
      const prov = (data.last_capture.provider || "").toUpperCase();
      lastCaptureEl.textContent = `${prov} · ${timeAgo(data.last_capture.at)}`;
    }
  }

  function statCell(num, cap, cls) {
    const node = statCellTpl.content.cloneNode(true);
    const numEl = node.querySelector(".stat-num");
    numEl.textContent = num;
    if (cls) numEl.classList.add(cls);
    node.querySelector(".stat-cap").textContent = cap;
    return node;
  }

  function renderStats(product, data) {
    statBlockEl.innerHTML = "";

    const head = document.createElement("div");
    head.className = "stat-head";
    const nameEl = document.createElement("span");
    nameEl.className = "stat-head-name " + (product === "array_operator" ? "ao" : "nepool");
    nameEl.textContent = product === "array_operator" ? "ARRAY OPERATOR" : "NEPOOL OPERATOR";
    head.appendChild(nameEl);
    if (data.company_name) {
      const coEl = document.createElement("span");
      coEl.className = "stat-head-co";
      coEl.textContent = data.company_name;
      head.appendChild(coEl);
    }
    statBlockEl.appendChild(head);

    const grid = document.createElement("div");
    grid.className = "stat-grid";

    if (product === "array_operator") {
      const ao = data.array_operator || {};
      grid.appendChild(statCell(fmtInt(ao.arrays), "Arrays"));
      grid.appendChild(statCell(fmtInt(ao.inverters), "Inverters"));
      const flagged = Number(ao.flagged || 0);
      grid.appendChild(statCell(fmtInt(flagged), "Flagged",
        flagged > 0 ? "flag-bad" : null));
      grid.appendChild(statCell(fmtKwh(ao.kwh_today), "kWh today"));
    } else {
      const np = data.nepool || {};
      grid.className = "stat-grid cols-3";
      grid.appendChild(statCell(fmtInt(np.clients), "Clients"));
      grid.appendChild(statCell(fmtInt(np.arrays), "Arrays"));
      grid.appendChild(statCell(np.last_report_at ? timeAgoShort(np.last_report_at) : "—",
        "Last report"));
    }

    statBlockEl.appendChild(grid);

    // Offtakers (per-offtaker invoicing plan, e.g. Paul) — a clean full-width
    // footer row only when the operator actually bills offtakers.
    if (product === "array_operator" && Number((data.array_operator || {}).offtakers || 0) > 0) {
      const foot = document.createElement("div");
      foot.className = "stat-foot";
      const k = document.createElement("span"); k.className = "stat-foot-k"; k.textContent = "Offtakers billed";
      const v = document.createElement("span"); v.className = "stat-foot-v";
      v.textContent = fmtInt(data.array_operator.offtakers);
      foot.appendChild(k); foot.appendChild(v);
      statBlockEl.appendChild(foot);
    }

    statBlockEl.classList.remove("hidden");
  }

  // ── Live toast on SO_CAPTURE_LANDED (while popup is open) ─────────────────
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type !== "SO_CAPTURE_LANDED") return;
    showToast(msg.ok);
  });

  function showToast(ok) {
    toastEl.textContent = ok ? "Captured" : "Capture failed";
    toastEl.classList.toggle("fail", !ok);
    toastEl.classList.remove("hidden", "fading");
    setTimeout(() => {
      toastEl.classList.add("fading");
      setTimeout(() => toastEl.classList.add("hidden"), 400);
    }, 3000);
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function fmtInt(n) {
    const v = Number(n);
    return Number.isFinite(v) ? String(Math.round(v)) : "—";
  }
  function fmtKwh(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    if (v >= 1000) return (v / 1000).toFixed(1) + "k";
    return v >= 100 ? String(Math.round(v)) : v.toFixed(0);
  }
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
  function timeAgoShort(iso) {
    const ms = Date.now() - new Date(iso).getTime();
    const h = Math.round(ms / 3600000);
    if (h < 1)  return "now";
    if (h < 24) return `${h}h`;
    const d = Math.round(h / 24);
    return `${d}d`;
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

  // ── Pending approvals (v1.9.109) ──────────────────────────────────────────
  // Page-initiated SO_VAULT set / SO_WIPE_COOKIES requests are intent-gated in
  // the background: the dashboard can only ASK, and the operation runs when the
  // owner clicks the confirm card here. One click = done; Dismiss = dropped.
  const pendingBlockEl = document.getElementById("pending-block");

  function credLabel(code) {
    const known = {
      fronius: "Fronius (Solar.web)", sma: "SMA (Sunny Portal)", chint: "Chint",
      gmp: "Green Mountain Power", vec: "Vermont Electric Co-op (SmartHub)",
      wec: "Washington Electric Co-op (SmartHub)",
    };
    if (known[code]) return known[code];
    try {
      const reg = window.SMARTHUB_REGISTRY;
      if (reg) { for (const host of Object.keys(reg)) { if (reg[host] && reg[host].provider === code) return (reg[host].name || code) + " (SmartHub)"; } }
    } catch (_) {}
    return String(code || "").toUpperCase();
  }
  function originHost(origin) {
    try { return new URL(origin).host || "your dashboard"; } catch (_) { return "your dashboard"; }
  }

  function pendingCard({ title, detail, confirmLabel, onConfirm, onDismiss }) {
    const card = document.createElement("div");
    card.className = "pending-card";
    const t = document.createElement("div"); t.className = "pending-title"; t.textContent = title;
    const d = document.createElement("div"); d.className = "pending-text"; d.textContent = detail;
    const actions = document.createElement("div"); actions.className = "pending-actions";
    const okBtn = document.createElement("button"); okBtn.className = "pending-ok"; okBtn.type = "button"; okBtn.textContent = confirmLabel;
    const noBtn = document.createElement("button"); noBtn.className = "pending-no"; noBtn.type = "button"; noBtn.textContent = "Dismiss";
    okBtn.addEventListener("click", async () => {
      okBtn.disabled = true; noBtn.disabled = true; okBtn.textContent = "Working…";
      const r = await onConfirm();
      okBtn.textContent = r && r.ok ? "✓ Done" : "Failed";
      setTimeout(renderPendingApprovals, 700);
    });
    noBtn.addEventListener("click", async () => {
      okBtn.disabled = true; noBtn.disabled = true;
      await onDismiss();
      renderPendingApprovals();
    });
    actions.appendChild(okBtn); actions.appendChild(noBtn);
    card.appendChild(t); card.appendChild(d); card.appendChild(actions);
    return card;
  }

  async function renderPendingApprovals() {
    if (!pendingBlockEl) return;
    const resp = await vaultMsg({ type: "SO_PENDING_LIST" });
    const vault = (resp && resp.ok && Array.isArray(resp.vault)) ? resp.vault : [];
    const wipes = (resp && resp.ok && Array.isArray(resp.wipes)) ? resp.wipes : [];
    pendingBlockEl.innerHTML = "";
    if (!vault.length && !wipes.length) { pendingBlockEl.classList.add("hidden"); return; }
    const head = document.createElement("div");
    head.className = "pending-head";
    head.textContent = "Waiting for your OK";
    pendingBlockEl.appendChild(head);
    for (const p of vault) {
      pendingBlockEl.appendChild(pendingCard({
        title: `Save your ${credLabel(p.vendor)} sign-in?`,
        detail: `${originHost(p.origin)} asked to save the login for ${p.username || "this portal"} so it can auto-refresh. Stored encrypted, only on this device.`,
        confirmLabel: "Save & turn on",
        onConfirm: () => vaultMsg({ type: "SO_PENDING_CONFIRM", kind: "vault", vendor: p.vendor }),
        onDismiss: () => vaultMsg({ type: "SO_PENDING_DISMISS", kind: "vault", vendor: p.vendor }),
      }));
    }
    for (const w of wipes) {
      pendingBlockEl.appendChild(pendingCard({
        title: `Reset your ${w.domain} session?`,
        detail: `${originHost(w.origin)} asked to sign this browser out of ${w.domain} so a different account can sign in. Open portal tabs will reload signed-out.`,
        confirmLabel: "Reset session",
        onConfirm: () => vaultMsg({ type: "SO_PENDING_CONFIRM", kind: "wipe", domain: w.domain }),
        onDismiss: () => vaultMsg({ type: "SO_PENDING_DISMISS", kind: "wipe", domain: w.domain }),
      }));
    }
    pendingBlockEl.classList.remove("hidden");
  }
  renderPendingApprovals();

  if (alToggle) {
    alToggle.addEventListener("click", () => {
      const open = alBody.classList.toggle("hidden") === false;
      alChev.textContent = open ? "▾" : "▸";
      alToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) renderAutoLogin();
    });
  }

  // Build one credential row (vendor OR utility) into `container`, re-rendering
  // `rerender` on any change. Shared by the inverter auto-login group and the
  // utility-logins group so they stay pixel-identical.
  function buildCredRow(container, item, status, rerender) {
    // Multi-login slots (v1.9.112): a row may represent one SAVED login (item.slot
    // + item.username, username fixed — remove & re-add to change it) or an
    // add/first row (no slot — saving writes via the code, and a new username
    // becomes its own slot instead of overwriting another client's login).
    const stateKey = item.slot || item.id;
    const st = (item.addRow ? null : status[stateKey]) || { hasCreds: false, enabled: true };
    const node = alTpl.content.cloneNode(true);
    const row = node.querySelector(".al-row");
    row.querySelector(".al-vendor").textContent = item.label;
    const stateEl = row.querySelector(".al-state");
    stateEl.textContent = item.addRow ? "" : (st.hasCreds ? (st.enabled ? "● saved · on" : "● saved · off") : "not set");
    stateEl.className = "al-state " + (st.hasCreds && st.enabled ? "on" : st.hasCreds ? "off" : "");
    const userEl = row.querySelector(".al-user");
    const passEl = row.querySelector(".al-pass");
    const saveBtn = row.querySelector(".al-save");
    const clearBtn = row.querySelector(".al-clear");
    const optCb = row.querySelector(".al-optout-cb");
    if (item.userPlaceholder) userEl.placeholder = item.userPlaceholder;
    if (item.username) { userEl.value = item.username; userEl.readOnly = true; userEl.title = "To change the username, remove this login and add it again."; }
    if (st.hasCreds) { passEl.placeholder = "•••••••• (saved — type to replace)"; clearBtn.classList.remove("hidden"); }
    optCb.checked = !st.enabled;   // checkbox = "off" (opted out)
    saveBtn.addEventListener("click", async () => {
      const u = (item.username || userEl.value).trim(); const p = passEl.value;
      if (!u || !p) { saveBtn.textContent = "enter both"; setTimeout(() => saveBtn.textContent = "Save", 1500); return; }
      saveBtn.textContent = "Saving…";
      const r = await vaultMsg({ type: "SO_VAULT_SET", vendor: item.id, username: u, password: p });
      saveBtn.textContent = r.ok ? "✓ Saved" : "failed";
      passEl.value = "";
      setTimeout(rerender, 900);
    });
    clearBtn.addEventListener("click", async () => {
      await vaultMsg({ type: "SO_VAULT_CLEAR", vendor: stateKey });
      rerender();
    });
    optCb.addEventListener("change", async () => {
      await vaultMsg({ type: "SO_VAULT_OPTOUT", vendor: stateKey, optedOut: optCb.checked });
      rerender();
    });
    container.appendChild(node);
  }

  async function renderAutoLogin() {
    const resp = await vaultMsg({ type: "SO_VAULT_STATUS" });
    const status = (resp && resp.status) || {};
    alRows.innerHTML = "";
    // Prefill the saved username on each inverter row (v1.9.120) so a saved login
    // shows the email it's saved under — like the utility rows already do — instead
    // of a blank field that reads as "not set". Remove & re-add to change it.
    for (const v of AL_VENDORS) {
      const st = status[v.id] || {};
      buildCredRow(alRows, Object.assign({}, v, { username: st.username || "" }), status, renderAutoLogin);
    }
  }

  // ── Utility logins (GMP + SmartHub co-ops) ────────────────────────────────
  // Same encrypted vault + same row UX as the inverter auto-login above. Offers
  // GMP + the grounded VT co-ops, and surfaces ANY other co-op the owner already
  // saved (a discovered sh_* one the backend resolved) from the vault status —
  // keyed by co-op code, the same key the background uses to pick the credential.
  const UT_VENDORS = [
    { id: "gmp", label: "Green Mountain Power", userPlaceholder: "GMP username / email" },
    { id: "vec", label: "Vermont Electric Co-op (SmartHub)", userPlaceholder: "SmartHub username / email" },
    { id: "wec", label: "Washington Electric Co-op (SmartHub)", userPlaceholder: "SmartHub username / email" },
  ];
  const utToggle = document.getElementById("ut-toggle");
  const utBody = document.getElementById("ut-body");
  const utChev = document.getElementById("ut-chev");
  const utRows = document.getElementById("ut-rows");

  if (utToggle) {
    utToggle.addEventListener("click", () => {
      const open = utBody.classList.toggle("hidden") === false;
      utChev.textContent = open ? "▾" : "▸";
      utToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) renderUtilityLogins();
    });
  }

  // Pretty label for a SmartHub co-op code we didn't pre-list, from the registry.
  function coopLabel(code) {
    try {
      const reg = window.SMARTHUB_REGISTRY;
      if (reg) { for (const host of Object.keys(reg)) { if (reg[host] && reg[host].provider === code) return (reg[host].name || code) + " (SmartHub)"; } }
    } catch (_) {}
    return code.toUpperCase() + " (SmartHub)";
  }

  // v1.9.112 — MULTI-LOGIN ROSTER. A utility can hold one saved login PER CLIENT
  // (vault slots). Render one row per saved login (username shown, fixed) plus an
  // "add another client login" row per utility, so a NEPOOL-agent operator can
  // hand the extension every client's portal login and go fully hands-off.
  async function renderUtilityLogins() {
    const resp = await vaultMsg({ type: "SO_VAULT_STATUS" });
    const status = (resp && resp.status) || {};
    utRows.innerHTML = "";
    // Group saved utility slots by code ("gmp" owns "gmp" + every "gmp::…").
    const byCode = {};
    for (const key of Object.keys(status)) {
      const e = status[key];
      if (!(e && e.utility && e.hasCreds)) continue;
      const code = e.code || key;
      (byCode[code] = byCode[code] || []).push({ slot: key, username: e.username || "" });
    }
    const codes = UT_VENDORS.map((v) => v.id);
    for (const code of Object.keys(byCode)) if (!codes.includes(code)) codes.push(code);
    for (const code of codes) {
      const pre = UT_VENDORS.find((v) => v.id === code);
      const label = pre ? pre.label : coopLabel(code);
      const placeholder = pre ? pre.userPlaceholder : "SmartHub username / email";
      const saved = (byCode[code] || []).slice().sort((a, b) => String(a.username).localeCompare(String(b.username)));
      for (const s of saved) {
        buildCredRow(utRows, {
          id: code, slot: s.slot, username: s.username,
          label: saved.length > 1 || s.username ? label : label,
          userPlaceholder: placeholder,
        }, status, renderUtilityLogins);
      }
      // The add/first row: empty fields; saving a NEW username adds a slot,
      // it never overwrites an existing client's login (vault.set semantics).
      buildCredRow(utRows, {
        id: code,
        label: saved.length ? label + " — add another client login" : label,
        userPlaceholder: placeholder,
        addRow: saved.length > 0,
      }, status, renderUtilityLogins);
    }
  }
})();
