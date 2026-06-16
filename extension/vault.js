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
  const CRED_STORE = "so_vault_creds";       // { fronius:{iv,ct}, sma:{...}, chint:{...} }
  const OPT_OUT_STORE = "so_autologin_optout"; // { fronius:true, ... } true = disabled
  const VENDORS = ["fronius", "sma", "chint"];

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
  async function set(vendor, username, password) {
    if (!VENDORS.includes(vendor)) return false;
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
  async function status() {
    const out = {};
    for (const v of VENDORS) {
      out[v] = { hasCreds: await has(v), enabled: await isEnabled(v) };
    }
    return out;
  }

  return { set, get, has, clear, isEnabled, setOptOut, status, VENDORS };
})();
