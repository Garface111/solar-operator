// ============================================================================
// vault.js — client-side encrypted credential vault for portal auto-login.
// ----------------------------------------------------------------------------
// SECURITY POSTURE (deliberate, per Ford's client-side BYOK call):
//   * Credentials NEVER leave this machine. They are encrypted with AES-GCM and
//     stored in chrome.storage.local. They are NEVER sent to the Array Operator
//     backend and NEVER appear in any network request to our servers. If our
//     servers are breached, there are ZERO customer portal passwords to steal.
//   * The encryption key is generated once per install (non-extractable would be
//     ideal, but we must persist it to decrypt across service-worker restarts, so
//     we store a per-install random key in chrome.storage.local alongside the
//     ciphertext). This protects against casual disk inspection / sync leakage of
//     the raw password, not against an attacker who already has full local profile
//     access (at which point the live portal session is compromised anyway).
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
  async function set(vendor, username, password) {
    if (!accepts(vendor)) return false;
    try {
      const key = await getKey();
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const plain = new TextEncoder().encode(JSON.stringify({ u: username, p: password }));
      const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plain);
      const s = await chrome.storage.local.get(CRED_STORE);
      const m = s[CRED_STORE] || {};
      m[vendor] = { iv: b64(iv), ct: b64(ct), at: Date.now() };
      await chrome.storage.local.set({ [CRED_STORE]: m });
      return true;
    } catch (e) {
      try { console.warn("[SoVault] set failed", vendor, e && e.message); } catch (_) {}
      return false;
    }
  }

  // Return {username, password} for a vendor, or null if none / decrypt fails.
  async function get(vendor) {
    try {
      const s = await chrome.storage.local.get(CRED_STORE);
      const rec = (s[CRED_STORE] || {})[vendor];
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

  // Auto-login is OPT-OUT: enabled unless explicitly disabled for that vendor.
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
  // Reports the inverter vendors AND every utility code that currently has creds
  // saved (so the popup can render the "Utility logins" group with live state —
  // including a discovered sh_* co-op the popup wouldn't otherwise list). The
  // popup keys utility rows it offers by code; here we surface whatever is stored.
  async function status() {
    const out = {};
    for (const v of VENDORS) {
      out[v] = { hasCreds: await has(v), enabled: await isEnabled(v) };
    }
    try {
      const s = await chrome.storage.local.get(CRED_STORE);
      const m = s[CRED_STORE] || {};
      for (const code of Object.keys(m)) {
        if (out[code]) continue;                 // already reported (inverter vendor)
        if (!isUtilityCode(code)) continue;      // ignore anything that isn't a utility code
        out[code] = { hasCreds: await has(code), enabled: await isEnabled(code), utility: true };
      }
    } catch (_) {}
    return out;
  }

  return { set, get, has, clear, isEnabled, setOptOut, status, VENDORS, UTILITIES, isUtilityCode, accepts };
})();
