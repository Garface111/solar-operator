// Regression tests for the Chint / CPS capture parsers.
// PURE helpers exported by chint_content.js. Fixtures mirror the header doc shapes:
//   busTypeDevices.data = { id, gwDevices:[ { commDevices:[ { assetTypeName:"Inverter",
//     sn, assetAlias, model, currentPower(W str), eToday(kWh num), statusName } ] } ] }
//   site/retrieve row = { id, siteName, installedCapacity(kW str), currentPower(W str),
//     weekETrend:[{name:"20260610", value:"996.2"}] }
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../chint_content.js");

function devJson(commDevices, siteId) {
  return { data: { id: siteId || "S1", gwDevices: [{ commDevices }] } };
}

test("parsePowerToW parses W/KW/MW units into watts", () => {
  assert.equal(C.parsePowerToW("72.7 KW"), 72700);   // doc example
  assert.equal(C.parsePowerToW("500 W"), 500);
  assert.equal(C.parsePowerToW("1.5 MW"), 1500000);
  assert.equal(C.parsePowerToW("0 kW"), 0);
  assert.equal(C.parsePowerToW("nonsense"), null);
  assert.equal(C.parsePowerToW(null), null);
});

test("num / kwFromStr coerce numeric strings, null on garbage", () => {
  assert.equal(C.num("42.5"), 42.5);
  assert.equal(C.num("nope"), null);
  assert.equal(C.kwFromStr("186.4"), 186.4);
  assert.equal(C.kwFromStr("bad"), null);
});

test("mapStatus classifies from statusName + power", () => {
  assert.equal(C.mapStatus("Normal", 5000), "producing");
  assert.equal(C.mapStatus("Normal", 0), "idle");
  assert.equal(C.mapStatus("Fault", 0), "fault");
  assert.equal(C.mapStatus("Alarm", 5000), "fault");     // fault keyword beats power
  assert.equal(C.mapStatus("Offline", 5000), "offline"); // off keyword beats power
  assert.equal(C.mapStatus("Standby", 0), "offline");
});

test("invertersFrom extracts inverter commDevices, skipping non-inverters", () => {
  const j = devJson([
    { assetTypeName: "Inverter", sn: "SN-1", assetAlias: "Inv 1", model: "CPS-100", currentPower: "50000", eToday: 120.5, statusName: "Normal" },
    { assetTypeName: "Meter", sn: "MTR-1", currentPower: "0", eToday: 0, statusName: "Normal" }, // not an inverter
    { assetType: 2, sn: "SN-2", assetAlias: "Inv 2", currentPower: "0", eToday: 0, statusName: "Offline" }, // inverter by assetType
  ]);
  const inv = C.invertersFrom(j);
  assert.equal(inv.length, 2);
  assert.equal(inv[0].serial, "SN-1");
  assert.equal(inv[0].energy_today_kwh, 120.5);
  assert.equal(inv[0].current_power_w, 50000);
  assert.equal(inv[0].status, "producing");
  assert.equal(inv[1].serial, "SN-2");
  assert.equal(inv[1].status, "offline");
});

test("invertersFrom drops a device with no serial/alias/id", () => {
  const j = devJson([{ assetTypeName: "Inverter", currentPower: "100", statusName: "Normal" }]);
  assert.equal(C.invertersFrom(j).length, 0);
});

test("invertersFrom holds an unexplained transient 0 after a real reading", () => {
  // First observe a genuine nonzero, then a 0 while status is NOT off/fault:
  // the parser omits current_power_w (null) so the backend keeps the prior good value.
  const good = devJson([{ assetTypeName: "Inverter", sn: "SN-9", currentPower: "8000", statusName: "Normal" }]);
  assert.equal(C.invertersFrom(good)[0].current_power_w, 8000);
  const blip = devJson([{ assetTypeName: "Inverter", sn: "SN-9", currentPower: "0", statusName: "Normal" }]);
  assert.equal(C.invertersFrom(blip)[0].current_power_w, null);
  // But a REAL off-state 0 is kept honestly.
  const offGood = devJson([{ assetTypeName: "Inverter", sn: "SN-10", currentPower: "5000", statusName: "Normal" }]);
  C.invertersFrom(offGood);
  const off = devJson([{ assetTypeName: "Inverter", sn: "SN-10", currentPower: "0", statusName: "Offline" }]);
  assert.equal(C.invertersFrom(off)[0].current_power_w, 0);
});

test("countInverters equals invertersFrom length", () => {
  const j = devJson([
    { assetTypeName: "Inverter", sn: "A", currentPower: "1", statusName: "Normal" },
    { assetTypeName: "Inverter", sn: "B", currentPower: "1", statusName: "Normal" },
  ]);
  assert.equal(C.countInverters(j), 2);
});

test("weekTrendDaily maps weekETrend YYYYMMDD rows to {date,kwh}", () => {
  const st = { weekETrend: [
    { name: "20260610", value: "996.2" },
    { name: "20260611", value: "1002.7" },
    { name: "bad", value: "5" },          // skipped
    { name: "20260612", value: "-3" },    // negative skipped
  ] };
  assert.deepEqual(C.weekTrendDaily(st), [
    { date: "2026-06-10", kwh: 996.2 },
    { date: "2026-06-11", kwh: 1002.7 },
  ]);
});

test("siteIdFromSearch extracts siteId from a query string", () => {
  assert.equal(C.siteIdFromSearch("?siteId=abc123&interval=30"), "abc123");
  assert.equal(C.siteIdFromSearch("?interval=30"), null);
});

test("dailyFromChart integrates a 30-min PV kW curve into daily kWh", () => {
  // 30-min steps -> stepH = 0.5. Two 4 kW slots in one day = 4*0.5 + 4*0.5 = 4 kWh.
  const json = { data: {
    times: ["2026-06-15 12:00", "2026-06-15 12:30", "2026-06-16 12:00"],
    pv: [4, 4, 10],
  } };
  const out = C.dailyFromChart(json, "?siteId=x&interval=30");
  assert.deepEqual(out, [
    { date: "2026-06-15", kwh: 4 },
    { date: "2026-06-16", kwh: 5 },   // single 10kW slot * 0.5h = 5
  ]);
});

test("dailyFromChart honors a non-default interval from the URL", () => {
  // interval=60 -> stepH = 1.0. One 4 kW slot = 4 kWh.
  const json = { data: { times: ["2026-06-15 12:00"], pv: [4] } };
  assert.deepEqual(C.dailyFromChart(json, "?siteId=x&interval=60"), [{ date: "2026-06-15", kwh: 4 }]);
});

test("dailyFromChart returns [] on empty/absent series", () => {
  assert.deepEqual(C.dailyFromChart({ data: { times: [] } }, ""), []);
  assert.deepEqual(C.dailyFromChart({}, ""), []);
  assert.deepEqual(C.dailyFromChart(null, ""), []);
});

test("mergeDaily unions series by date, max-wins, ascending", () => {
  const a = [{ date: "2026-06-11", kwh: 100 }, { date: "2026-06-10", kwh: 90 }];
  const b = [{ date: "2026-06-11", kwh: 105 }, { date: "2026-06-12", kwh: 80 }];
  assert.deepEqual(C.mergeDaily(a, b), [
    { date: "2026-06-10", kwh: 90 },
    { date: "2026-06-11", kwh: 105 },   // max of 100/105
    { date: "2026-06-12", kwh: 80 },
  ]);
});
