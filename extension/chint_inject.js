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

  // ── Forced re-fetch (relayed from chint_content.js) ──────────────────────────
  // The isolated-world content script can't make the SPA re-issue its OWN authed
  // requests (replaying the per-request token 4010s). Here in the MAIN world we
  // bounce the app's hash route so it re-enters and re-fetches with its valid token;
  // the passive hooks above then observe the fresh responses. We ONLY drive the
  // app's OWN router — we NEVER craft, replay, or modify an API request.
  try {
    window.addEventListener("message", function (e) {
      if (e.source !== window || e.origin !== location.origin) return;
      var d = e.data;
      if (!d || d.type !== "SO_CHINT_FORCE_REFRESH") return;
      try {
        // Go to the SITES LIST route — THIS is what fetches /api/asset/site/retrieve (the
        // owner's sites + ids); the dashboard/overview route does NOT (confirmed live
        // 2026-06-27: location.hash="#/pv/sites" fired "observed SITE LIST"). Once the
        // content script sees the list it kicks the per-site walk below.
        var sites = "#/pv/sites";
        if (location.hash.indexOf(sites) !== 0) {
          location.hash = sites;                               // navigate to the sites list
        } else {
          location.hash = sites + "?r=" + Date.now();          // already there → cache-bust re-enter
          setTimeout(function () { try { location.hash = sites; } catch (_) {} }, 60);
        }
        L("force-refresh: -> sites list (#/pv/sites)");
      } catch (_) {}
    });
    L("force-refresh listener installed");
  } catch (e) { L("force-refresh listener FAILED", e && e.message); }

  // ── Site-walk (v1.9.77) ──────────────────────────────────────────────────────
  // Drive the app's OWN router through each site's DETAIL route so the SPA fetches that
  // site's busTypeDevices with its own valid token — the click-free equivalent of the
  // owner opening every site. Confirmed live (2026-06-27, Bruce's account): setting
  // location.hash = "#/pv/sites/siteDetail/<id>" PROGRAMMATICALLY (no click) fires
  // busTypeDevices and our hooks observe it. Still read-only: we only change the SPA's
  // route, never craft/replay an API request. Single-flight; returns home when done.
  try {
    // OBSERVE-DRIVEN walk: chint_content posts SO_CHINT_SITE_OBSERVED the moment it sees a
    // site's busTypeDevices RESPONSE. We track that here and advance the walk as soon as the
    // current site's devices have landed — instead of a blind fixed timer that sometimes
    // moved on BEFORE the response arrived (→ empty capture → stuck). A min dwell lets the SPA
    // fire the request; a max cap stops a dead/slow site from hanging the whole walk.
    var _chintObserved = {};
    window.addEventListener("message", function (e) {
      if (e.source !== window || e.origin !== location.origin) return;
      var d = e.data;
      if (d && d.type === "SO_CHINT_SITE_OBSERVED" && d.siteId) { _chintObserved[String(d.siteId)] = true; }
    });
    window.addEventListener("message", function (e) {
      if (e.source !== window || e.origin !== location.origin) return;
      var d = e.data;
      if (!d || d.type !== "SO_CHINT_WALK_SITES" || !Array.isArray(d.ids)) return;
      if (window.__soChintWalking) return;                 // don't stack walks
      // Walk EVERY site the owner has. This used to be `.slice(0, 50)` ("sane cap"),
      // which silently dropped all inverters/production for sites 51+ (and left the
      // content script's _walkExpected at the untruncated count) -- a Chint operator
      // with >50 sites lost that data with no signal, and the capture self-reported
      // "complete". The walk only bounces the app's OWN hash router (the same fetch a
      // human clicking a site triggers) -- there is no vendor rate limit here, so a
      // larger fleet just takes longer, not unbounded compute (Ford, 2026-07-09:
      // completeness over thrift). A ceiling far above any real fleet backstops a bug.
      var ids = d.ids.slice(0, 2000);
      if (d.ids.length > 2000) {
        L("walk: WARNING truncating an implausible", d.ids.length, "site list to 2000");
      }
      if (!ids.length) return;
      window.__soChintWalking = true;
      var MIN_MS = 500, MAX_MS = 7000, POLL_MS = 250;
      L("walk: stepping through", ids.length, "site(s) (no click, observe-driven)");
      function walkOne(i) {
        if (i >= ids.length) {
          try { location.hash = "#/pv/sites"; } catch (_) {}   // return to the sites list when done
          window.__soChintWalking = false;
          // Tell the content script every site has been visited, so a silent (recap/sync-all)
          // capture knows the snapshot is COMPLETE and can close its surface — without this a
          // multi-site owner would be truncated at the first site's emit.
          try { window.postMessage({ type: "SO_CHINT_WALK_DONE" }, location.origin); } catch (_) {}
          L("walk: done");
          return;
        }
        var id = String(ids[i]);
        try { location.hash = "#/pv/sites/siteDetail/" + id; } catch (_) {}
        L("walk: -> site", id);
        var waited = 0;
        (function waitObserved() {
          if (_chintObserved[id] && waited >= MIN_MS) { setTimeout(function () { walkOne(i + 1); }, 200); return; }
          if (waited >= MAX_MS) { L("walk: site", id, "timed out waiting for devices — advancing"); walkOne(i + 1); return; }
          waited += POLL_MS;
          setTimeout(waitObserved, POLL_MS);
        })();
      }
      walkOne(0);
    });
    L("site-walk listener installed");
  } catch (e) { L("site-walk listener FAILED", e && e.message); }
})();
