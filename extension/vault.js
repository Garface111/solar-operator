// ============================================================================
// vault.js — client-side encrypted credential vault for portal auto-login.
// ----------------------------------------------------------------------------
// SECURITY POSTURE (deliberate, per Ford's client-side BYOK call):
//   * Credentials NEVER leave this machine. They are encrypted with AES-GCM and
//     stored in chrome.storage.local. They are NEVER sent to the Array Operator
//     backend and NEVER appear in any network request to our servers. If our
//     servers are breached, there are ZERO customer portal passwords to steal.
//   * ⚠️ HONESTY — THIS IS OBFUSCATION-AT-REST, NOT REAL ENCRYPTION-AT-REST.
//     The AES-256 key is generated once per install and persisted in
//     chrome.storage.local (`so_vault_key`) RIGHT BESIDE the ciphertext
//     (`so_vault_creds`). Anyone who can read the extension's storage on disk can
//     read both and recover the passwords. Why we knowingly live with that in MV3:
//       - Chrome extensions have NO OS-keychain access (no DPAPI/Keychain/libsecret
//         API surface), so there is nowhere non-colocated to root a key.
//       - Any derivation input we could use instead (extension id, profile paths,
//         install time) sits on the same disk with the same readability — it adds
//         indirection, not protection.
//       - A non-extractable CryptoKey in IndexedDB would stop JS-context key export
//         and split key from ciphertext across stores, but Chrome still persists the
//         key material in the profile directory (same-disk attacker still wins), and
//         IndexedDB for an extension SW can be EVICTED under storage pressure — an
//         evicted key silently bricks every saved login, killing the flagship
//         "password once, never sign in again" feature. Marginal gain, real risk.
//       - A user master-password (real KDF-rooted encryption) would defeat the whole
//         point of hands-off auto-login.
//     What the AES layer DOES buy: the raw password never sits as plaintext in
//     storage dumps/logs/exports, and a leak of the cred blob ALONE (without the key
//     record) is useless. What it does NOT buy: protection from an attacker with
//     full local profile access — but that attacker already owns the live portal
//     sessions (cookies) anyway. See extension/README.md "Vault security posture".
//   * Auto-login is OPT-OUT (default ON) and per-vendor; the owner can clear a
//     vendor's creds at any time, which deletes the ciphertext.
//
// This file is loaded into the background service worker via importScripts at the
// TOP of background.js, so SoVault is available before any handler runs.
// ============================================================================
const SoVault = (() => {
  const KEY_STORE = "so_vault_key";          // { k: base64 raw AES-256 key }
  const CRED_STORE = "so_vault_creds";       // { fronius:{iv,ct}, sma:{...}, chint:{...}, gmp:{...}, vec:{...} }
  const OPT_OUT_STORE = "so_autologin_optout"; // { fronius:true, ... } true = disabled
  const VENDORS = ["fronius", "sma", "chint"];

  // v1.9.97: the vault also stores UTILITY portal creds so a utility session
  // (GMP, or any SmartHub co-op) can be silently re-authed for hands-off bill
  // pulls — exactly like the inverter vendors. Utility codes are NOT in VENDORS
  // (that list still drives the inverter-only paths). A code is a valid utility
  // when it's "gmp", a known SmartHub co-op code (vec/wec/…), or a discovered
  // "sh_*" co-op (smarthub_registry.js mints "sh_<subdomain>" for any new co-op).
  // The known-utility list is intentionally small + curated; the sh_ prefix +
  // the SMARTHUB_CODES allowlist (populated from the registry when available)
  // cover the long tail so a brand-new co-op works without a vault change.
  const UTILITIES = ["gmp"];
  // Provider codes seen in smarthub_registry.js (vec, wec, …). Populated lazily
  // from the registry if it's loaded in this context (popup loads it; the SW
  // imports the SW-safe build). Falls back to the sh_ prefix + a couple of
  // grounded codes (vec/wec) so utility creds work even without the registry.
  const SMARTHUB_FALLBACK_CODES = new Set(["vec", "wec"]);
  function _smarthubCodes() {
    try {
      const reg = (typeof self !== "undefined" && self.SMARTHUB_REGISTRY)
        || (typeof window !== "undefined" && window.SMARTHUB_REGISTRY) || null;
      if (reg) {
        const s = new Set(SMARTHUB_FALLBACK_CODES);
        for (const k of Object.keys(reg)) { const c = reg[k] && reg[k].provider; if (c) s.add(String(c).toLowerCase()); }
        return s;
      }
    } catch (_) {}
    return SMARTHUB_FALLBACK_CODES;
  }
  // True if `code` is a utility portal credential code (GMP or any SmartHub co-op).
  function isUtilityCode(code) {
    const c = String(code || "").toLowerCase();
    if (!c) return false;
    if (UTILITIES.includes(c)) return true;
    if (c.startsWith("sh_")) return true;       // any discovered co-op
    return _smarthubCodes().has(c);             // known co-op (vec/wec/… from the registry)
  }
  // The gate every vault op uses: an inverter vendor OR a utility code.
  function accepts(code) { return VENDORS.includes(code) || isUtilityCode(code); }

  // ── Multi-login slots (v1.9.112) ──────────────────────────────────────────
  // A NEPOOL-agent operator holds a SEPARATE portal login per client, so a
  // utility code can now own several credential slots. Slot key format:
  //   "<code>"                 — legacy/default slot (Bruce's install: untouched)
  //   "<code>::<lc username>"  — each additional login for that utility
  // Multi-slot is UTILITY-ONLY: inverter vendors keep exactly one slot (their
  // live loops are single-account by design). All existing per-key ops
  // (get/clear/isEnabled) already read arbitrary keys, so slots ride free.
  const SLOT_SEP = "::";
  function slotCode(slotKey) {
    const s = String(slotKey || "");
    const i = s.indexOf(SLOT_SEP);
    return i === -1 ? s : s.slice(0, i);
  }
  function slotKeyFor(code, username) {
    return String(code) + SLOT_SEP + String(username || "").trim().toLowerCase();
  }

  function b64(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  function unb64(str) {
    const bin = atob(str);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  async function getKey() {
    const s = await chrome.storage.local.get(KEY_STORE);
    let rec = s[KEY_STORE];
    if (!rec || !rec.k) {
      const raw = crypto.getRandomValues(new Uint8Array(32)); // AES-256
      rec = { k: b64(raw) };
      await chrome.storage.local.set({ [KEY_STORE]: rec });
    }
    return crypto.subtle.importKey("raw", unb64(rec.k), { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
  }

  // Store a vendor's {username, password}, encrypted. Returns true on success.
  // `vendor` may be an inverter vendor (fronius/sma/chint) OR a utility code
  // (gmp / a SmartHub co-op code). get/clear/isEnabled/setOptOut are keyed the
  // same way and never gate (read/toggle/delete an arbitrary key is harmless);
  // only set() validates the key so we never persist creds under a junk code.
  //
  // Utility codes are MULTI-SLOT: the same username overwrites its own slot;
  // a NEW username gets its own "<code>::<username>" slot. The first login
  // ever saved for a code lands on the plain "<code>" key, byte-identical to
  // pre-multi-slot behavior, so existing installs and callers see no change.
  async function set(vendor, username, password) {
    if (!accepts(vendor)) return false;
    try {
      let slot = vendor;
      if (isUtilityCode(vendor)) {
        const existing = await list(vendor);
        const uLc = String(username || "").trim().toLowerCase();
        const match = existing.find((e) => String(e.username || "").trim().toLowerCase() === uLc);
        if (match) slot = match.slot;                       // same login → overwrite its slot
        else if (existing.length > 0) slot = slotKeyFor(vendor, username);  // additional login → own slot
        // else: first login for this code → plain "<code>" slot (legacy behavior)
      }
      const key = await getKey();
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const plain = new TextEncoder().encode(JSON.stringify({ u: username, p: password }));
      const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plain);
      const s = await chrome.storage.local.get(CRED_STORE);
      const m = s[CRED_STORE] || {};
      m[slot] = { iv: b64(iv), ct: b64(ct), at: Date.now() };
      await chrome.storage.local.set({ [CRED_STORE]: m });
      return true;
    } catch (e) {
      try { console.warn("[SoVault] set failed", vendor, e && e.message); } catch (_) {}
      return false;
    }
  }

  // Return {username, password} for a vendor/slot key, or null if none.
  // For a UTILITY code whose plain slot is gone but that still has "::" slots
  // (e.g. the first-saved login was removed), falls back to the oldest slot so
  // "is there a usable login?" callers keep working.
  async function get(vendor) {
    try {
      const s = await chrome.storage.local.get(CRED_STORE);
      const m = s[CRED_STORE] || {};
      let rec = m[vendor];
      if ((!rec || !rec.iv || !rec.ct) && isUtilityCode(vendor) && String(vendor).indexOf(SLOT_SEP) === -1) {
        const pfx = vendor + SLOT_SEP;
        const alt = Object.keys(m).filter((k) => k.startsWith(pfx))
          .sort((a, b) => (m[a].at || 0) - (m[b].at || 0))[0];
        if (alt) rec = m[alt];
      }
      if (!rec || !rec.iv || !rec.ct) return null;
      const key = await getKey();
      const plain = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: unb64(rec.iv) }, key, unb64(rec.ct));
      const obj = JSON.parse(new TextDecoder().decode(plain));
      return { username: obj.u, password: obj.p };
    } catch (e) {
      try { console.warn("[SoVault] get failed", vendor, e && e.message); } catch (_) {}
      return null;
    }
  }

  // Every saved login for a code, oldest-first: [{slot, username, at}].
  // Decrypts each slot to read the username (usernames are never stored in the
  // clear in the cred store). For an inverter vendor this is just 0 or 1 rows.
  async function list(code) {
    const out = [];
    try {
      const s = await chrome.storage.local.get(CRED_STORE);
      const m = s[CRED_STORE] || {};
      const pfx = code + SLOT_SEP;
      const slots = Object.keys(m).filter((k) => k === code || k.startsWith(pfx))
        .sort((a, b) => (m[a].at || 0) - (m[b].at || 0));
      for (const slot of slots) {
        const rec = m[slot];
        if (!rec || !rec.iv || !rec.ct) continue;
        try {
          const key = await getKey();
          const plain = await crypto.subtle.decrypt(
            { name: "AES-GCM", iv: unb64(rec.iv) }, key, unb64(rec.ct));
          const obj = JSON.parse(new TextDecoder().decode(plain));
          out.push({ slot, username: obj.u || "", at: rec.at || 0 });
        } catch (_) { /* undecryptable slot — skip, never throw the whole list away */ }
      }
    } catch (e) {
      try { console.warn("[SoVault] list failed", code, e && e.message); } catch (_) {}
    }
    return out;
  }

  async function has(vendor) { return !!(await get(vendor)); }

  async function clear(vendor) {
    try {
      const s = await chrome.storage.local.get(CRED_STORE);
      const m = s[CRED_STORE] || {};
      delete m[vendor];
      await chrome.storage.local.set({ [CRED_STORE]: m });
      return true;
    } catch (_) { return false; }
  }

  // ── Page-initiated save intents (v1.9.109) ────────────────────────────────
  // SO_VAULT op:"set" arriving from PAGE context (the so_bridge relay) no longer
  // writes the vault directly — a page script (XSS / compromised dep on a legit
  // app origin) must never be able to overwrite the owner's saved portal logins.
  // Instead the request is STASHED here (password AES-encrypted with the same
  // vault key, never plaintext at rest) and committed only when the owner clicks
  // Save in the extension popup — a user gesture inside extension UI that a web
  // page cannot fake. One pending intent per vendor code; newest wins.
  const PENDING_STORE = "so_vault_pending";  // { <code>: {u, iv, ct, origin, at} }

  async function stashPending(vendor, username, password, origin) {
    if (!accepts(vendor)) return false;
    if (!username || !password) return false;
    try {
      const key = await getKey();
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const plain = new TextEncoder().encode(JSON.stringify({ u: username, p: password }));
      const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plain);
      const s = await chrome.storage.local.get(PENDING_STORE);
      const m = s[PENDING_STORE] || {};
      // `u` is kept in the clear ONLY for the popup's confirm card ("save the
      // sign-in for <username>?") — the password is never stored unencrypted.
      m[vendor] = { u: String(username), iv: b64(iv), ct: b64(ct), origin: String(origin || ""), at: Date.now() };
      await chrome.storage.local.set({ [PENDING_STORE]: m });
      return true;
    } catch (e) {
      try { console.warn("[SoVault] stashPending failed", vendor, e && e.message); } catch (_) {}
      return false;
    }
  }

  // [{vendor, username, origin, at}] for the popup confirm card. No passwords.
  async function listPending() {
    try {
      const s = await chrome.storage.local.get(PENDING_STORE);
      const m = s[PENDING_STORE] || {};
      return Object.keys(m).map((vendor) => ({
        vendor, username: m[vendor].u || "", origin: m[vendor].origin || "", at: m[vendor].at || 0,
      }));
    } catch (_) { return []; }
  }

  // Decrypt + REMOVE a pending intent (popup confirm path). Returns
  // {username, password} or null. The caller commits via set().
  async function takePending(vendor) {
    try {
      const s = await chrome.storage.local.get(PENDING_STORE);
      const m = s[PENDING_STORE] || {};
      const rec = m[vendor];
      if (!rec || !rec.iv || !rec.ct) return null;
      delete m[vendor];
      await chrome.storage.local.set({ [PENDING_STORE]: m });
      const key = await getKey();
      const plain = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: unb64(rec.iv) }, key, unb64(rec.ct));
      const obj = JSON.parse(new TextDecoder().decode(plain));
      return { username: obj.u, password: obj.p };
    } catch (e) {
      try { console.warn("[SoVault] takePending failed", vendor, e && e.message); } catch (_) {}
      return null;
    }
  }

  async function dismissPending(vendor) {
    try {
      const s = await chrome.storage.local.get(PENDING_STORE);
      const m = s[PENDING_STORE] || {};
      delete m[vendor];
      await chrome.storage.local.set({ [PENDING_STORE]: m });
      return true;
    } catch (_) { return false; }
  }

  // Auto-login is OPT-OUT: enabled unless explicitly disabled for that key.
  // Multi-slot note: each login's opt-out is ITS OWN — the plain "<code>" slot's
  // toggle governs only that login, never the sibling "<code>::" slots (a master
  // switch keyed on the code would make the first login's "off" silently disable
  // every other client's login, since the plain slot key IS the code).
  async function isEnabled(vendor) {
    const s = await chrome.storage.local.get(OPT_OUT_STORE);
    return !((s[OPT_OUT_STORE] || {})[vendor] === true);
  }
  async function setOptOut(vendor, optedOut) {
    const s = await chrome.storage.local.get(OPT_OUT_STORE);
    const m = s[OPT_OUT_STORE] || {};
    m[vendor] = !!optedOut;
    await chrome.storage.local.set({ [OPT_OUT_STORE]: m });
  }

  // Lightweight status for the popup UI (never returns the actual secrets).
  // Reports the inverter vendors AND every utility SLOT that currently has creds
  // saved (so the popup can render the "Utility logins" group with live state —
  // including a discovered sh_* co-op the popup wouldn't otherwise list). Utility
  // entries are keyed by SLOT key and carry {code, username} so the popup can
  // group several logins under one utility.
  async function status() {
    const out = {};
    for (const v of VENDORS) {
      // Include the saved username for inverters too (v1.9.120) — utilities already
      // report it, and both the popup + the Master Account panel need it to show a
      // saved login as clearly present (an email in the field), not a blank row.
      let username = "";
      try { const rec = await get(v); username = (rec && rec.username) || ""; } catch (_) {}
      out[v] = { hasCreds: await has(v), enabled: await isEnabled(v), username };
    }
    try {
      const s = await chrome.storage.local.get(CRED_STORE);
      const m = s[CRED_STORE] || {};
      for (const slot of Object.keys(m)) {
        if (out[slot]) continue;                 // already reported (inverter vendor)
        const code = slotCode(slot);
        if (!isUtilityCode(code)) continue;      // ignore anything that isn't a utility code
        let username = "";
        try { const rec = await get(slot); username = (rec && rec.username) || ""; } catch (_) {}
        out[slot] = {
          hasCreds: !!(m[slot] && m[slot].iv && m[slot].ct),
          enabled: await isEnabled(slot),
          utility: true, code, username,
        };
      }
    } catch (_) {}
    return out;
  }

  return { set, get, has, clear, isEnabled, setOptOut, status, list, slotCode, slotKeyFor,
           VENDORS, UTILITIES, isUtilityCode, accepts,
           stashPending, listPending, takePending, dismissPending };
})();

// Browser-inert test hook (Node regression harness only — see extension/tests/).
if (typeof module !== "undefined" && module.exports) module.exports = SoVault;
