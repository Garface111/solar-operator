// Regression tests for the Fronius (Solar.web) capture parsers.
// Exercises the PURE helpers exported by solarweb_content.js — no browser, no fetch.
// Fixtures mirror the exact JSON shapes documented in the content script header.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("../solarweb_content.js");

test("parseAspNetDate parses ASP.NET /Date(ms)/ to ISO", () => {
  assert.equal(F.parseAspNetDate("/Date(1781542800000)/"), new Date(1781542800000).toISOString());
  assert.equal(F.parseAspNetDate("not a date"), null);
  assert.equal(F.parseAspNetDate(null), null);
  assert.equal(F.parseAspNetDate(12345), null);
});

test("nameplateFromModel takes the first decimal as kW", () => {
  // Doc examples: "Primo 12.5-1 208-240" -> 12.5, "Symo 20.0-3-M" -> 20.0
  assert.equal(F.nameplateFromModel("Primo 12.5-1 208-240"), 12.5);
  assert.equal(F.nameplateFromModel("Symo 20.0-3-M"), 20);
  assert.equal(F.nameplateFromModel("Primo 8.2-1"), 8.2);
  // "Gen24": the digits abut a letter (no word boundary), so the (\d…) regex
  // does not match -> null. Documents the real edge, not an idealized one.
  assert.equal(F.nameplateFromModel("Gen24"), null);
  assert.equal(F.nameplateFromModel("GEN 24"), 24);       // space -> boundary -> matches
  assert.equal(F.nameplateFromModel("Inverter"), null);   // no number
  assert.equal(F.nameplateFromModel(null), null);
});

test("deriveNameplateKw = energyToday / specific-yield, rounded to 0.1", () => {
  // 50 kWh at 5 kWh/kWp -> 10 kWp
  assert.equal(F.deriveNameplateKw(50, 5), 10);
  assert.equal(F.deriveNameplateKw(53, 4.9), 10.8);       // 10.816 -> 10.8
  assert.equal(F.deriveNameplateKw(0, 5), null);          // no production
  assert.equal(F.deriveNameplateKw(50, 0), null);         // zero yield -> guard
  assert.equal(F.deriveNameplateKw(null, 5), null);
});

test("deriveStatus maps power/error/online to a plain status", () => {
  assert.equal(F.deriveStatus(0, 0, false), "offline");   // online===false wins
  assert.equal(F.deriveStatus(1000, 0, true), "producing");
  assert.equal(F.deriveStatus(0, 3, true), "fault");      // error count > 0
  assert.equal(F.deriveStatus(0, 0, true), "idle");       // online, no error, night
  assert.equal(F.deriveStatus(500, 2, true), "fault");    // fault beats producing
});

test("integrateKwh trapezoid-integrates a [ts_ms, kW] series", () => {
  const hour = 3600000;
  const t0 = Date.UTC(2026, 5, 20, 12, 0, 0);
  // Flat 4 kW for 2 hours -> 8 kWh (two 1h trapezoids of avg 4).
  const flat = [[t0, 4], [t0 + hour, 4], [t0 + 2 * hour, 4]];
  assert.equal(F.integrateKwh(flat), 8);
  // Ramp 0 -> 4 -> 0 over 2h -> (avg2 *1)+(avg2*1) = 4 kWh.
  const ramp = [[t0, 0], [t0 + hour, 4], [t0 + 2 * hour, 0]];
  assert.equal(F.integrateKwh(ramp), 4);
});

test("integrateKwh skips gaps > 1h and non-numeric points (data-gap guard)", () => {
  const hour = 3600000;
  const t0 = Date.UTC(2026, 5, 20, 12, 0, 0);
  // A 2h gap between the two 4 kW points must NOT be integrated.
  const gapped = [[t0, 4], [t0 + 2 * hour, 4]];
  assert.equal(F.integrateKwh(gapped), 0);
  // null / missing values are filtered out before integration.
  const withNull = [[t0, 4], [t0 + hour, null], [t0 + hour, 4], [t0 + 2 * hour, 4]];
  // Nulls are dropped, leaving 4kW at t0, t0+1h, t0+2h -> two 1h trapezoids = 8 kWh.
  assert.equal(F.integrateKwh(withNull), 8);
  assert.equal(F.integrateKwh([]), 0);
  assert.equal(F.integrateKwh(null), 0);
});

test("integrateKwh sorts unordered points before integrating", () => {
  const hour = 3600000;
  const t0 = Date.UTC(2026, 5, 20, 12, 0, 0);
  const unordered = [[t0 + 2 * hour, 4], [t0, 4], [t0 + hour, 4]];
  assert.equal(F.integrateKwh(unordered), 8);
});

test("findLocation pulls a plausible lat/lng from nested JSON", () => {
  // Direct sibling lat/lng.
  assert.deepEqual(F.findLocation({ latitude: 44.26, longitude: -72.58 }),
    { latitude: 44.26, longitude: -72.58 });
  // Buried under a preferred key.
  const nested = { data: { site: { location: { lat: 43.6, lng: -72.3 } } } };
  assert.deepEqual(F.findLocation(nested), { latitude: 43.6, longitude: -72.3 });
});

test("findLocation reads a bare [x,y] pair as GeoJSON [lng,lat]", () => {
  // GeoJSON order is [lng, lat]; -72 is a valid lng, 44 a valid lat.
  assert.deepEqual(F.findLocation({ coordinates: [-72.58, 44.26] }),
    { latitude: 44.26, longitude: -72.58 });
});

test("findLocation rejects null-island and out-of-range coords", () => {
  assert.equal(F.findLocation({ latitude: 0, longitude: 0 }), null);
  assert.equal(F.findLocation({ latitude: 999, longitude: 999 }), null);
  assert.equal(F.findLocation(null), null);
  assert.equal(F.findLocation("nope"), null);
});

test("findLocation falls back to a joined address string when no coords", () => {
  const withAddr = { site: { city: "Waterford", state: "VT", zip: "05819" } };
  assert.deepEqual(F.findLocation(withAddr), { address: "Waterford, VT, 05819" });
});

test("findLocation attaches address alongside coords when both present", () => {
  const both = { location: { latitude: 44.26, longitude: -72.58, address: "1 Main St, Waterford VT" } };
  assert.deepEqual(F.findLocation(both),
    { latitude: 44.26, longitude: -72.58, address: "1 Main St, Waterford VT" });
});

test("applyLocation merges coords/address onto a site in place, additively", () => {
  const site = { name: "Waterford" };
  F.applyLocation(site, { latitude: 44.26, longitude: -72.58, address: "VT" });
  assert.equal(site.latitude, 44.26);
  assert.equal(site.longitude, -72.58);
  assert.equal(site.address, "VT");
  // Does not clobber an existing address.
  const site2 = { address: "existing" };
  F.applyLocation(site2, { address: "new" });
  assert.equal(site2.address, "existing");
});
