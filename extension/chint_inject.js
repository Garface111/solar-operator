// chint_inject.js — runs in the PAGE's MAIN world on monitor.chintpowersystems.com.
//
// Passively observes the Chint app's OWN successful API responses (we can't
// replay its per-request-bound encrypted token, so we read the data it already
// fetched) and relays them to chint_content.js (isolated world).
//
// DIAGNOSTIC build: logs that it loaded + every API URL it intercepts, so we can
// confirm the hooks fire and see exactly which endpoints this page calls.
//
// SAFETY: read-only — never blocks/modifies/adds requests; only copies response
// text for owner-data endpoints and relays it same-window.
(function () {
  "use strict";
  if (!/(^|\.)chintpowersystems\.com$/.test(location.hostname)) return;

  var DBG = true;
  var DBG_VERBOSE = true;    // diagnostic: log EVERY api response path so we can see if busTypeDevices fires
  function L() { if (DBG) { try { console.log.apply(console, ["[EnergyAgent CHINT inject]"].concat([].slice.call(arguments))); } catch (e) {} } }
  L("LOADED (MAIN world) on", location.href);

  // Relay the response body of any API data endpoint we care about. We match
  // broadly (/api/asset/ + /openApi/) and tag with the pathname so the content
  // script picks what it needs.
  function interesting(pathname) {
    return /^\/api\/asset\//.test(pathname) || /^\/openApi\//.test(pathname) || /^\/api\/users\/user\//.test(pathname);
  }
  function handle(url, getText) {
    var u; try { u = new URL(url, location.href); } catch (e) { return; }
    var isApi = /^\/api\//.test(u.pathname) || /^\/openApi\//.test(u.pathname);
    if (isApi && DBG_VERBOSE) L("API response seen:", u.pathname + (u.search || ""));
    if (!interesting(u.pathname)) return;
    Promise.resolve(getText()).then(function (txt) {
      if (!txt) return;
      try {
        window.postMessage({ type: "SO_CHINT_RESPONSE", path: u.pathname, search: u.search, body: String(txt) }, location.origin);
      } catch (e) { /* ignore */ }
    }).catch(function () {});
  }

  // ── Hook XMLHttpRequest ──
  try {
    var oOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, url) { try { this.__so_url = url; } catch (e) {} return oOpen.apply(this, arguments); };
    var oSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function () {
      var self = this;
      try {
        this.addEventListener("load", function () {
          handle(self.__so_url || self.responseURL || "", function () {
            try { return (self.responseType === "" || self.responseType === "text") ? self.responseText : (self.response ? JSON.stringify(self.response) : ""); }
            catch (e) { try { return self.responseText; } catch (e2) { return ""; } }
          });
        });
      } catch (e) {}
      return oSend.apply(this, arguments);
    };
    L("XHR hook installed");
  } catch (e) { L("XHR hook FAILED", e && e.message); }

  // ── Hook fetch ──
  try {
    var oFetch = window.fetch;
    window.fetch = function (input, init) {
      var url = (typeof input === "string") ? input : (input && input.url);
      var p = oFetch.apply(this, arguments);
      try { p.then(function (resp) { handle(url || "", function () { return resp.clone().text(); }); }).catch(function () {}); } catch (e) {}
      return p;
    };
    L("fetch hook installed");
  } catch (e) { L("fetch hook FAILED", e && e.message); }
})();
