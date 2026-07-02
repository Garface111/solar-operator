// Regression tests for the SMA (ennexOS / Sunny Portal) capture parsers.
// PURE helpers exported by sunnyportal_content.js — no browser, no Bearer fetch.
// Device shapes mirror the header doc: /api/v1/overview/{plantId}/devices rows like
//   { serial, product:"STP 24kTL-US-10", name:"#4 24kW 191245395", pvPower, state }.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../sunnyportal_content.js");

test("nameplateKw parses the STP product string", () => {
  // Doc example: "STP 24kTL-US-10" -> 24
  assert.equal(S.nameplateKw("STP 24kTL-US-10", null), 24);
  assert.equal(S.nameplateKw("STP 50kTL-US-10", "whatever"), 50);
  assert.equal(S.nameplateKw("STP 15.0kTL", null), 15);
});

test("nameplateKw falls back to the device name kW token", () => {
  // Doc example name: "#4 24kW 191245395" -> 24 when product is unhelpful.
  assert.equal(S.nameplateKw(null, "#4 24kW 191245395"), 24);
  assert.equal(S.nameplateKw("Datamanager", "#2 33.3 kW 55"), 33.3);
  assert.equal(S.nameplateKw("nothing numeric", "no rating here"), null);
  assert.equal(S.nameplateKw(null, null), null);
});

test("deriveStatus keys off ennexOS device state 307 = OK", () => {
  assert.equal(S.deriveStatus({ state: 307, pvPower: 5000 }), "producing");
  assert.equal(S.deriveStatus({ state: 307, pvPower: 0 }), "idle");     // OK but night
  assert.equal(S.deriveStatus({ state: 455, pvPower: 5000 }), "fault"); // any non-307 = fault
  assert.equal(S.deriveStatus({ state: null, pvPower: 3000 }), "producing"); // no state -> power-based
  assert.equal(S.deriveStatus({ state: null, pvPower: 0 }), "idle");
});

test("shared findLocation deep-scans an ennexOS plant payload", () => {
  // A plausible /api/v1/plants/{id} shape with coords under a "location" key.
  const plant = { plantId: "8296660", name: "Timberworks",
    plantOperator: { name: "GMCS" }, location: { latitude: 44.51, longitude: -72.02 } };
  assert.deepEqual(S.findLocation(plant), { latitude: 44.51, longitude: -72.02 });
});

test("shared _soValidLatLng guards range and null-island", () => {
  assert.deepEqual(S._soValidLatLng(44.5, -72.0), { latitude: 44.5, longitude: -72.0 });
  assert.deepEqual(S._soValidLatLng("44.5", "-72.0"), { latitude: 44.5, longitude: -72.0 }); // string coercion
  assert.equal(S._soValidLatLng(0, 0), null);        // null island
  assert.equal(S._soValidLatLng(91, 10), null);      // lat out of range
  assert.equal(S._soValidLatLng(10, 181), null);     // lng out of range
  assert.equal(S._soValidLatLng("x", 10), null);     // non-numeric
});
