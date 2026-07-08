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

// ── C9 regression: single-inverter series shipped as the SITE daily history ──
// Prod ground truth 2026-07-03 (Timberworks 150kW, plant 8296660): the plant
// history query came back holding inverter #1's ~180 kWh days as the site
// series (true site day ≈ 1,276 kWh) → the Analysis spotlight showed
// "made 180 kWh vs 930 expected — 19%" against GMP's metered 1,282.2.

const CH = "Measurement.Metering.TotWhOut.Pv";

test("pickHistorySeries prefers the componentId we asked for", () => {
  const res = [
    // first entry = a DEVICE series (the old code blindly took this one)
    { componentId: "14993829", channelId: CH, values: [{ time: "2026-06-29T04:00:00Z", value: 179590 }] },
    { componentId: "8296660", channelId: CH, values: [{ time: "2026-06-29T04:00:00Z", value: 1275870 }] },
  ];
  assert.equal(S.pickHistorySeries(res, "8296660").values[0].value, 1275870);
  // asking for the device gets the device series
  assert.equal(S.pickHistorySeries(res, "14993829").values[0].value, 179590);
});

test("pickHistorySeries falls back sanely on single-series shapes", () => {
  // no componentId on the series (older shape) -> channel match still works
  const noId = [{ channelId: CH, values: [{ time: "t", value: 1 }] }];
  assert.equal(S.pickHistorySeries(noId, "8296660").values[0].value, 1);
  // no channel either -> any series with a values array
  const bare = [{ values: [{ time: "t", value: 2 }] }];
  assert.equal(S.pickHistorySeries(bare, "8296660").values[0].value, 2);
  assert.equal(S.pickHistorySeries(null, "8296660"), null);
  assert.equal(S.pickHistorySeries([], "8296660"), null);
});

test("reconcileSiteDaily lifts a single-inverter site series to the inverter sum", () => {
  // The exact Timberworks shape: site 'daily' = inverter #1's numbers, while
  // the per-inverter histories carry the real comb for the same dates.
  const perInv = [
    [179.59, 205.69], [178.10, 204.37], [177.82, 203.93], [110.80, 127.42],
    [172.96, 199.59], [114.02, 130.33], [178.15, 204.54],
  ];
  const inverters = perInv.map(([d28, d29], i) => ({
    serial: "s" + i,
    daily: [{ date: "2026-06-28", kwh: d28 }, { date: "2026-06-29", kwh: d29 }],
  }));
  const badSite = [
    { date: "2026-06-28", kwh: 149.78 },   // inverter #1's series, not the site's
    { date: "2026-06-29", kwh: 179.59 },
  ];
  const out = S.reconcileSiteDaily(badSite, inverters);
  const byDate = Object.fromEntries(out.map((p) => [p.date, p.kwh]));
  assert.equal(byDate["2026-06-28"], 1111.44);
  assert.equal(byDate["2026-06-29"], 1275.87);
});

test("reconcileSiteDaily keeps an already-correct site series", () => {
  const inverters = [
    { daily: [{ date: "2026-06-29", kwh: 205.69 }] },
    { daily: [{ date: "2026-06-29", kwh: 204.37 }] },
  ];
  // site total (all 7 inverters) is LARGER than this partial 2-inverter sum
  const site = [{ date: "2026-06-29", kwh: 1275.87 }];
  assert.deepEqual(S.reconcileSiteDaily(site, inverters),
    [{ date: "2026-06-29", kwh: 1275.87 }]);
});

test("reconcileSiteDaily fills dates only the inverter histories know", () => {
  const inverters = [
    { daily: [{ date: "2026-06-27", kwh: 100 }, { date: "2026-06-28", kwh: 110 }] },
    { daily: [{ date: "2026-06-27", kwh: 200 }] },
    { daily: null },                                     // tolerated
  ];
  const out = S.reconcileSiteDaily([], inverters);
  assert.deepEqual(out, [
    { date: "2026-06-27", kwh: 300 },
    { date: "2026-06-28", kwh: 110 },
  ]);
});

test("reconcileSiteDaily ignores junk points", () => {
  const site = [{ date: "2026-06-29", kwh: -5 }, { date: null, kwh: 10 }, null];
  const inverters = [{ daily: [{ date: "2026-06-29", kwh: NaN }, { date: "2026-06-29", kwh: 42 }] }];
  assert.deepEqual(S.reconcileSiteDaily(site, inverters),
    [{ date: "2026-06-29", kwh: 42 }]);
});

// ── decodeJwtEmail: the zero-typing bridge to the official SMA consent flow ──
// (the owner's email is read off their OWN Keycloak Bearer token, never typed).
function fakeJwt(payload) {
  const b64url = (obj) => Buffer.from(JSON.stringify(obj))
    .toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64url({ alg: "RS256" })}.${b64url(payload)}.fakesig`;
}

test("decodeJwtEmail reads the email claim off a real-shaped Keycloak token", () => {
  const tok = fakeJwt({ sub: "abc123", email: "owner@example.com", exp: 9999999999 });
  assert.equal(S.decodeJwtEmail(tok), "owner@example.com");
});

test("decodeJwtEmail never throws on garbage input", () => {
  assert.equal(S.decodeJwtEmail(null), null);
  assert.equal(S.decodeJwtEmail(""), null);
  assert.equal(S.decodeJwtEmail("not-a-jwt"), null);
  assert.equal(S.decodeJwtEmail("a.b"), null);                         // payload isn't valid base64 JSON
  assert.equal(S.decodeJwtEmail(fakeJwt({ sub: "no-email-claim" })), null);
  assert.equal(S.decodeJwtEmail(fakeJwt({ email: "not-an-email" })), null); // rejects a malformed claim
});
