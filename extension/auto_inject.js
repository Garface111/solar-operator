// EnergyAgent generic capture hook (MAIN world). Injected by auto_content.js ONLY
// when the auto-adapter pilot flag is on. Transparently wraps fetch + XHR to read
// the JSON responses the portal itself fetches (the user's own session), and
// forwards likely-data bodies to the isolated content script via window.postMessage.
// Never touches credentials, cookies, or auth headers - only response bodies the
// page already received.
(function () {
  "use strict";
  function post(fmt, body) { try { window.postMessage({ __ea_auto: true, fmt: fmt, body: body }, "*"); } catch (e) {} }
  function looksData(t) {
    return typeof t === "string" && t.length > 40 &&
      (t.trim().charAt(0) === "{" || t.trim().charAt(0) === "[") &&
      /\b\d{4}\b|kwh|[^a-z]wh\b|energy|power|production|export|generation|pvsystem|inverter|usage|meter|received/i.test(t);
  }
  var of = window.fetch;
  if (of) {
    window.fetch = function () {
      var p = of.apply(this, arguments);
      try {
        p.then(function (r) {
          try { r.clone().text().then(function (t) { if (looksData(t)) post("json", t); }); } catch (e) {}
          return r;
        });
      } catch (e) {}
      return p;
    };
  }
  var os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    try {
      this.addEventListener("load", function () {
        try { if (looksData(this.responseText)) post("json", this.responseText); } catch (e) {}
      });
    } catch (e) {}
    return os.apply(this, arguments);
  };
})();
