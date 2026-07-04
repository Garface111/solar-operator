// Regression tests for SoVault multi-login slots (v1.9.112).
// A NEPOOL-agent operator holds a separate portal login per client, so a
// utility code can own several credential slots ("gmp", "gmp::<username>", …).
// The FIRST login saved for a code must land on the plain "<code>" key —
// byte-identical to pre-multi-slot behavior — so existing installs (Bruce)
// and single-login callers see no change. Inverter vendors stay single-slot.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");

const HAS_CRYPTO = !!(globalThis.crypto && globalThis.crypto.subtle);

// ── chrome.storage.local stub (in-memory, promise API like MV3) ─────────────
const _store = {};
global.chrome = {
  storage: {
    local: {
      async get(keys) {
        const out = {};
        const list = Array.isArray(keys) ? keys : [keys];
        for (const k of list) if (k in _store) out[k] = _store[k];
        return out;
      },
      async set(obj) { Object.assign(_store, obj); },
    },
  },
};

const SoVault = require("../vault.js");

test("first utility login lands on the plain code slot (legacy shape)", { skip: !HAS_CRYPTO }, async () => {
  assert.equal(await SoVault.set("gmp", "bruce@gmcs.com", "pw-one"), true);
  const m = _store.so_vault_creds;
  assert.deepEqual(Object.keys(m), ["gmp"]);           // no "::" key for the first login
  const rec = await SoVault.get("gmp");
  assert.deepEqual(rec, { username: "bruce@gmcs.com", password: "pw-one" });
});

test("a second username gets its own slot; the first is untouched", { skip: !HAS_CRYPTO }, async () => {
  assert.equal(await SoVault.set("gmp", "Client-Two@example.com", "pw-two"), true);
  const slots = Object.keys(_store.so_vault_creds).sort();
  assert.deepEqual(slots, ["gmp", "gmp::client-two@example.com"]);
  assert.equal((await SoVault.get("gmp")).password, "pw-one");
  assert.equal((await SoVault.get("gmp::client-two@example.com")).password, "pw-two");
});

test("re-saving an existing username overwrites its own slot, adds nothing", { skip: !HAS_CRYPTO }, async () => {
  // Case-insensitive match: same login typed with different casing.
  assert.equal(await SoVault.set("gmp", "client-two@EXAMPLE.com", "pw-two-rotated"), true);
  assert.equal(Object.keys(_store.so_vault_creds).length, 2);
  assert.equal((await SoVault.get("gmp::client-two@example.com")).password, "pw-two-rotated");
  // The plain slot's username can be overwritten too.
  assert.equal(await SoVault.set("gmp", "bruce@gmcs.com", "pw-one-rotated"), true);
  assert.equal(Object.keys(_store.so_vault_creds).length, 2);
  assert.equal((await SoVault.get("gmp")).password, "pw-one-rotated");
});

test("list() enumerates every login for a code (oldest save first)", { skip: !HAS_CRYPTO }, async () => {
  const logins = await SoVault.list("gmp");
  assert.equal(logins.length, 2);
  // Order is by last-save time (an overwrite refreshes `at`), so assert content.
  assert.deepEqual(logins.map((l) => l.slot).sort(), ["gmp", "gmp::client-two@example.com"]);
  assert.deepEqual(logins.map((l) => l.username).sort(), ["bruce@gmcs.com", "client-two@EXAMPLE.com"]);
  // Another code's slots never leak in.
  assert.deepEqual(await SoVault.list("vec"), []);
});

test("clearing the plain slot: get(code) falls back to a remaining slot", { skip: !HAS_CRYPTO }, async () => {
  assert.equal(await SoVault.clear("gmp"), true);
  const rec = await SoVault.get("gmp");
  assert.equal(rec.password, "pw-two-rotated");        // oldest surviving slot
  // And the NEXT save must not resurrect a duplicate for that same username.
  assert.equal(await SoVault.set("gmp", "client-two@example.com", "pw-final"), true);
  assert.deepEqual(Object.keys(_store.so_vault_creds), ["gmp::client-two@example.com"]);
});

test("opt-out is strictly per slot — the plain slot never masters siblings", { skip: !HAS_CRYPTO }, async () => {
  await SoVault.setOptOut("gmp", true);
  // Turning the plain "gmp" slot off must NOT disable a sibling client's login.
  assert.equal(await SoVault.isEnabled("gmp::client-two@example.com"), true);
  assert.equal(await SoVault.isEnabled("gmp"), false);
  await SoVault.setOptOut("gmp", false);
  // Per-slot opt-out works on its own key.
  await SoVault.setOptOut("gmp::client-two@example.com", true);
  assert.equal(await SoVault.isEnabled("gmp::client-two@example.com"), false);
  assert.equal(await SoVault.isEnabled("gmp"), true);
  await SoVault.setOptOut("gmp::client-two@example.com", false);
});

test("status() reports utility slots with code + username, never passwords", { skip: !HAS_CRYPTO }, async () => {
  const st = await SoVault.status();
  const slot = st["gmp::client-two@example.com"];
  assert.ok(slot);
  assert.equal(slot.utility, true);
  assert.equal(slot.code, "gmp");
  assert.equal(slot.username, "client-two@example.com");
  assert.equal(slot.hasCreds, true);
  assert.equal(JSON.stringify(st).includes("pw-final"), false);
});

test("inverter vendors stay single-slot (second save overwrites)", { skip: !HAS_CRYPTO }, async () => {
  assert.equal(await SoVault.set("fronius", "a@x.com", "p1"), true);
  assert.equal(await SoVault.set("fronius", "b@y.com", "p2"), true);
  const keys = Object.keys(_store.so_vault_creds).filter((k) => k.startsWith("fronius"));
  assert.deepEqual(keys, ["fronius"]);
  assert.deepEqual(await SoVault.get("fronius"), { username: "b@y.com", password: "p2" });
});
