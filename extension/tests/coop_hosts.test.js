// Regression tests for the learned SmartHub co-op host map (coop_hosts.js).
//
// This is the fix that lets background bill/generation refresh reach DISCOVERED
// co-ops (sh_<sub>, not in the curated registry): we learn a co-op's real host
// from its own capture, then _utilityPortalUrl() resolves a background-open URL
// from it. These tests pin the pure resolution + the host-validation guard (we
// must never learn / background-open an arbitrary non-smarthub URL).
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

// Fresh module instance per require — reset the global so each require re-inits
// the in-memory map (the module attaches to globalThis and short-circuits if the
// export already exists is NOT how it works, but delete keeps tests independent).
function freshModule() {
  delete require.cache[require.resolve("../coop_hosts.js")];
  try { delete globalThis.SoCoopHosts; } catch (_) {}
  return require("../coop_hosts.js");
}

test("learns a discovered co-op host and resolves a portal URL", () => {
  const C = freshModule();
  assert.equal(C.urlFor("sh_missoulaelectric"), null); // unknown until learned
  assert.equal(C.record("sh_missoulaelectric", "missoulaelectric.smarthub.coop"), true);
  assert.equal(C.urlFor("sh_missoulaelectric"), "https://missoulaelectric.smarthub.coop/");
});

test("record is case-insensitive on code and host, idempotent", () => {
  const C = freshModule();
  assert.equal(C.record("SH_Foo", "FooCoop.SmartHub.Coop"), true);
  assert.equal(C.urlFor("sh_foo"), "https://foocoop.smarthub.coop/");
  assert.equal(C.record("sh_foo", "foocoop.smarthub.coop"), false); // no change → no re-persist
});

test("extracts the host from a full URL", () => {
  const C = freshModule();
  assert.equal(C.record("sh_bar", "https://barcoop.smarthub.coop/ui/#/login"), true);
  assert.equal(C.urlFor("sh_bar"), "https://barcoop.smarthub.coop/");
});

test("rejects any host that is not a *.smarthub.coop deployment (anti-spoof)", () => {
  const C = freshModule();
  // A spoofed capture message must NEVER teach us to background-open evil.com.
  assert.equal(C.record("sh_evil", "evil.com"), false);
  assert.equal(C.record("sh_evil", "https://evil.com/smarthub.coop"), false);
  assert.equal(C.record("sh_evil", "smarthub.coop.evil.com"), false);
  assert.equal(C.record("sh_evil", ""), false);
  assert.equal(C.urlFor("sh_evil"), null);
});

test("load() bulk-hydrates a persisted map and skips junk", () => {
  const C = freshModule();
  C.load({ sh_a: "acoop.smarthub.coop", sh_b: "bad", sh_c: "ccoop.smarthub.coop" });
  assert.equal(C.urlFor("sh_a"), "https://acoop.smarthub.coop/");
  assert.equal(C.urlFor("sh_b"), null);          // "bad" rejected by normHost
  assert.equal(C.urlFor("sh_c"), "https://ccoop.smarthub.coop/");
});

test("all() round-trips through load() (persistence contract)", () => {
  const C = freshModule();
  C.record("vec", "vermontelectric.smarthub.coop");
  C.record("wec", "washingtonelectric.smarthub.coop");
  const snapshot = C.all();
  const D = freshModule();
  D.load(snapshot);
  assert.equal(D.urlFor("vec"), "https://vermontelectric.smarthub.coop/");
  assert.equal(D.urlFor("wec"), "https://washingtonelectric.smarthub.coop/");
});
