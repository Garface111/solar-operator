// EnergyAgent generic adapter interpreter (classic content-script build).
// Executes a declarative spec (downloaded from /v1/adapters) against a captured
// portal response, locally in the extension's isolated world. JSON only (the
// overwhelming majority of live portal XHR). Mirrors api/auto_adapters.py.
// Exposes globalThis.AutoAdapter = { fingerprint, extract, validate, parseDate }.
(function (g) {
  "use strict";

  function getDot(obj, path) {
    var cur = obj, parts = path.split("."), i;
    for (i = 0; i < parts.length; i++) {
      cur = (cur && typeof cur === "object") ? cur[parts[i]] : undefined;
      if (cur === undefined || cur === null) return null;
    }
    return cur;
  }

  function jsonRecords(root, steps) {
    var nodes = [root], si, ni, segs, sj, coll, arr, ai, item, st, ok, k;
    for (si = 0; si < steps.length; si++) {
      st = steps[si];
      var nxt = [];
      for (ni = 0; ni < nodes.length; ni++) {
        coll = nodes[ni];
        segs = st.path.split(".");
        for (sj = 0; sj < segs.length; sj++) {
          coll = (coll && typeof coll === "object") ? coll[segs[sj]] : undefined;
        }
        if (coll === undefined || coll === null) continue;
        arr = Array.isArray(coll) ? coll : [coll];
        for (ai = 0; ai < arr.length; ai++) {
          item = arr[ai];
          if (st.where) {
            ok = true;
            for (k in st.where) { if (String(item[k]) !== String(st.where[k])) { ok = false; break; } }
            if (!ok) continue;
          }
          nxt.push(item);
        }
      }
      nodes = nxt;
    }
    return nodes;
  }

  function parseDate(v, kind) {
    if (v === null || v === undefined) return null;
    try {
      if (kind === "dotnet") { var m = String(v).match(/(\d{10,})/); return m ? new Date(Number(m[1])).toISOString().slice(0, 10) : null; }
      if (kind === "epoch_ms") return new Date(Number(v)).toISOString().slice(0, 10);
      if (kind === "epoch_s") return new Date(Number(v) * 1000).toISOString().slice(0, 10);
      var s = String(v).trim().split(/\s+/)[0];
      if (kind === "my") { var a = s.split("/"); return a[1] + "-" + ("0" + a[0]).slice(-2); }
      if (kind === "mdy") { var b = s.split("/"); return b[2] + "-" + ("0" + b[0]).slice(-2) + "-" + ("0" + b[1]).slice(-2); }
      return s.slice(0, 10);
    } catch (e) { return null; }
  }

  function r3(x) { return Math.round(x * 1000) / 1000; }

  function extract(spec, raw) {
    if (spec.format !== "json") throw new Error("client runs JSON adapters; XML handled server-side");
    var root = (typeof raw === "string") ? JSON.parse(raw) : raw;
    var recs = jsonRecords(root, spec.records), fd = spec.fields, out = [], i, rrec, gpath, g;
    for (i = 0; i < recs.length; i++) {
      rrec = recs[i];
      gpath = fd.generation_kwh.path;
      g = getDot(rrec, gpath);
      if (g === null || g === undefined) continue;
      out.push({
        date: parseDate(getDot(rrec, fd.date.path), fd.date.parse || "iso"),
        generation_kwh: r3(Number(g) * Number(fd.generation_kwh.scale != null ? fd.generation_kwh.scale : 1))
      });
    }
    var computed = r3(out.reduce(function (a, x) { return a + x.generation_kwh; }, 0));
    var summary = null, st = spec.summary_total_kwh, sv;
    if (st) {
      sv = getDot(root, st.path);
      if (sv !== null && sv !== undefined) summary = r3(Number(sv) * Number(st.scale != null ? st.scale : 1));
    }
    return { records: out, computed: computed, summary: summary };
  }

  function validate(records, computed, summary) {
    var hard = [], notes = [], i, dates = [];
    if (!records.length) hard.push("no records");
    for (i = 0; i < records.length; i++) {
      if (records[i].generation_kwh < 0 || records[i].generation_kwh > 100000) { hard.push("implausible value"); break; }
    }
    for (i = 0; i < records.length; i++) dates.push(records[i].date);
    if (dates.some(function (d) { return !d; })) hard.push("unparseable date");
    var uniq = {}; for (i = 0; i < dates.length; i++) uniq[dates[i]] = 1;
    if (Object.keys(uniq).length !== dates.length) notes.push("duplicate dates (multi-site snapshot or possible double-count)");
    var delta = null;
    if (summary !== null) {
      delta = summary ? Math.abs(computed - summary) / summary : 1.0;
      if (delta > 0.02) hard.push("reconcile mismatch " + (delta * 100).toFixed(1) + "%");
    }
    return { ok: hard.length === 0, reasons: hard.concat(notes), delta: delta };
  }

  function fingerprint(raw, fmt) {
    if (fmt === "xml") { var m = String(raw).match(/<([\w:]+)[\s>]/); return "xml:" + (m ? m[1].split(":").pop() : "?"); }
    var obj = (typeof raw === "string") ? JSON.parse(raw) : raw;
    var keys = (obj && typeof obj === "object" && !Array.isArray(obj)) ? Object.keys(obj).sort().join(",") : "list";
    return "json:" + keys;
  }

  g.AutoAdapter = { getDot: getDot, jsonRecords: jsonRecords, parseDate: parseDate, extract: extract, validate: validate, fingerprint: fingerprint };
})(typeof globalThis !== "undefined" ? globalThis : this);
