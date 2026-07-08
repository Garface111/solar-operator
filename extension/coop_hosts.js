// coop_hosts.js — learned SmartHub co-op host map (v1.9.115).
//
// WHY: background bill/generation refresh opens a co-op's portal in a background
// tab, resolving the portal URL from a co-op CODE via _utilityPortalUrl(). For the
// ~530 co-ops in the curated registry (smarthub_registry.js) that resolves fine.
// But a DISCOVERED co-op — any *.smarthub.coop deployment not yet in the catalog,
// captured once in the foreground and given a deterministic "sh_<subdomain>" code
// by the server — has NO registry entry, so _utilityPortalUrl returned null and
// recaptureVendor() bailed: it could be captured once but NEVER auto-refreshed.
// That's the long tail of "every utility we support" that silently didn't stay
// fresh.
//
// FIX: we already run ON the co-op's real host every time its content script
// captures. Learn code→host from that capture (and from the connect click that
// opens the portal) and remember it, so background refresh reaches the entire tail,
// not just the curated catalog. Pure + framework-free so it loads in the service
// worker (importScripts → self.SoCoopHosts) AND in the node test harness
// (require → module.exports). Persistence (chrome.storage) is the caller's job;
// this module only owns the in-memory map + the resolution rules.
(function (glob) {
  "use strict";

  var _hosts = {};   // { <lowercase code>: "<sub>.smarthub.coop" }

  // A host we'll trust for a background open: a bare *.smarthub.coop hostname
  // (no scheme, no path, no port). Anything else → "" (rejected), so we never
  // background-open an arbitrary URL learned from a spoofed message.
  function normHost(h) {
    h = String(h == null ? "" : h).trim().toLowerCase();
    if (!h) return "";
    // tolerate a full URL by extracting the hostname
    var m = h.match(/^https?:\/\/([^/:?#]+)/);
    if (m) h = m[1];
    h = h.replace(/[/:?#].*$/, "");
    if (!/^[a-z0-9.-]+\.smarthub\.coop$/.test(h)) return "";
    return h;
  }

  // Learn code→host. Returns true iff the map changed (so the caller can persist).
  function record(code, host) {
    code = String(code == null ? "" : code).trim().toLowerCase();
    host = normHost(host);
    if (!code || !host) return false;
    if (_hosts[code] === host) return false;
    _hosts[code] = host;
    return true;
  }

  // Portal URL for a learned co-op code, or null if we've never seen its host.
  function urlFor(code) {
    var h = _hosts[String(code == null ? "" : code).trim().toLowerCase()];
    return h ? ("https://" + h + "/") : null;
  }

  // Bulk-load a persisted map (from chrome.storage) at startup. Skips junk.
  function load(obj) {
    if (!obj || typeof obj !== "object") return;
    for (var k in obj) { if (Object.prototype.hasOwnProperty.call(obj, k)) record(k, obj[k]); }
  }

  function all() {
    var out = {};
    for (var k in _hosts) { if (Object.prototype.hasOwnProperty.call(_hosts, k)) out[k] = _hosts[k]; }
    return out;
  }

  glob.SoCoopHosts = { record: record, urlFor: urlFor, load: load, all: all, normHost: normHost };
})(typeof self !== "undefined" ? self
  : typeof globalThis !== "undefined" ? globalThis
  : this);

if (typeof module !== "undefined" && module.exports) {
  module.exports = (typeof self !== "undefined" ? self
    : typeof globalThis !== "undefined" ? globalThis
    : this).SoCoopHosts;
}
