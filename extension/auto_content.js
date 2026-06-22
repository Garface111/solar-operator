// EnergyAgent generic auto-adapter capture (isolated world).
//
// The data-driven alternative to a per-portal content script. When the pilot flag
// `so_auto_adapter` is ON, this captures the portal's own JSON responses, fingerprints
// the platform, asks the backend for an approved declarative adapter, runs it LOCALLY
// (auto_interpreter.js), and pushes the normalized generation to the fleet. If no
// adapter exists yet, it uploads the capture so the backend can synthesize one. A new
// portal therefore needs NO new content script - just an approved adapter (data).
//
// Default OFF: with the flag unset this script does nothing (no hook injected, no
// network), so the proven per-portal scripts are completely unaffected. Pilot host
// is SolarWeb only (see manifest).
(function () {
  "use strict";
  var PILOT_HOSTS = ["www.solarweb.com", "solarweb.com"];
  var FLAG_KEY = "so_auto_adapter";
  var specCache = {};      // fingerprint -> spec (this page session)
  var inFlight = {};       // fingerprint -> true while ingesting

  function base(s) {
    var ep = s.api_endpoint || "https://nepooloperator.com/v1/sync";
    return ep.replace(/\/v1\/sync\/?$/, "");
  }
  function headers(s) {
    var h = { "Content-Type": "application/json" };
    if (s.tenant_key) h["Authorization"] = "Bearer " + s.tenant_key;
    return h;
  }

  chrome.storage.local.get([FLAG_KEY, "api_endpoint", "tenant_key"], function (s) {
    s = s || {};
    if (s[FLAG_KEY] !== true) return;                              // FLAG OFF -> fully inert
    if (PILOT_HOSTS.indexOf(location.hostname) === -1) return;     // pilot host only

    var sc = document.createElement("script");                    // inject the MAIN-world hook
    sc.src = chrome.runtime.getURL("auto_inject.js");
    (document.head || document.documentElement).appendChild(sc);
    sc.remove();

    window.addEventListener("message", function (e) {
      if (e.source !== window || !e.data || e.data.__ea_auto !== true) return;
      handle(e.data.fmt, e.data.body, s);
    });
  });

  function handle(fmt, raw, s) {
    var AA = globalThis.AutoAdapter;
    if (!AA) return;
    var fp;
    try { fp = AA.fingerprint(raw, fmt); } catch (e) { return; }
    if (specCache[fp]) return run(specCache[fp], raw, fp, s);
    if (inFlight[fp]) return;

    fetch(base(s) + "/v1/adapters/lookup?fp=" + encodeURIComponent(fp), { headers: headers(s) })
      .then(function (r) { return r.status === 200 ? r.json() : null; })
      .then(function (spec) {
        if (spec) { specCache[fp] = spec; run(spec, raw, fp, s); return; }
        inFlight[fp] = true;                                       // unknown platform -> ask backend to synthesize
        return fetch(base(s) + "/v1/adapters/ingest", {
          method: "POST", headers: headers(s),
          body: JSON.stringify({ capture: raw, fmt: fmt, source: location.hostname })
        }).then(function () { inFlight[fp] = false; });
      })
      .catch(function () { inFlight[fp] = false; });
  }

  function run(spec, raw, fp, s) {
    var AA = globalThis.AutoAdapter;
    try {
      var out = AA.extract(spec, raw);
      var v = AA.validate(out.records, out.computed, out.summary);
      if (!v.ok || !out.records.length) return;
      fetch(base(s) + "/v1/adapters/readings", {
        method: "POST", headers: headers(s),
        body: JSON.stringify({ source: location.hostname, fingerprint: fp, records: out.records })
      }).catch(function () {});
    } catch (e) {}
  }
})();
