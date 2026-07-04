// Regression tests for the SolarEdge capture parsers.
// PURE helpers extracted from solaredge_content.js. SolarEdge is mostly cookie-authed
// fetches; the testable derivations are the api-key body parse (JSON / object /
// bare-token) and the searchSites page -> site shape mapping.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const SE = require("../solaredge_content.js");

test("_parseApiKey reads a bare ~32-char alphanumeric token", () => {
  assert.equal(SE._parseApiKey("ABCDEFGH12345678ABCDEFGH"), "ABCDEFGH12345678ABCDEFGH");
  assert.equal(SE._parseApiKey("  ABCDEFGH12345678ABCDEFGH  "), "ABCDEFGH12345678ABCDEFGH"); // trimmed
  assert.equal(SE._parseApiKey("short"), null);          // too short / not a token
  assert.equal(SE._parseApiKey(""), null);
  assert.equal(SE._parseApiKey(null), null);
});

test("_parseApiKey reads a JSON string body", () => {
  assert.equal(SE._parseApiKey(JSON.stringify("MYKEY12345678MYKEY123456")), "MYKEY12345678MYKEY123456");
});

test("_parseApiKey scans a JSON object for a *key*-ish field", () => {
  assert.equal(SE._parseApiKey(JSON.stringify({ apiKey: "TOPLEVELKEY0001" })), "TOPLEVELKEY0001");
  assert.equal(SE._parseApiKey(JSON.stringify({ note: "x", accountKey: "NESTEDNAME0002" })), "NESTEDNAME0002");
  assert.equal(SE._parseApiKey(JSON.stringify({ data: { api_key: "UNDERDATA0003" } })), "UNDERDATA0003");
  assert.equal(SE._parseApiKey(JSON.stringify({ nothing: "here" })), null);
});

test("_mapSites maps searchSites page rows to the site shape", () => {
  const page = [
    { solarFieldId: 111, name: "Rooftop A", peakPower: 9.9, status: "Active", inverterCount: 1 },
    { solarFieldId: 222, name: "Ground B", peakPower: 33.3, status: "Active", inverterCount: 3 },
  ];
  assert.deepEqual(SE._mapSites(page), [
    { site_id: 111, name: "Rooftop A", peak_power_kw: 9.9, status: "Active", inverter_count: 1 },
    { site_id: 222, name: "Ground B", peak_power_kw: 33.3, status: "Active", inverter_count: 3 },
  ]);
});

test("_mapSites tolerates a non-array / empty page", () => {
  assert.deepEqual(SE._mapSites(null), []);
  assert.deepEqual(SE._mapSites(undefined), []);
  assert.deepEqual(SE._mapSites([]), []);
});
