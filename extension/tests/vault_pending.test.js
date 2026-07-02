// Regression tests for the SoVault pending-intent stash (v1.9.109 bridge hardening).
// A page-relayed SO_VAULT op:"set" must STASH (encrypted) — never write the vault —
// until the owner confirms in the popup. Exercises vault.js in Node with a stubbed
// chrome.storage.local + Node's built-in WebCrypto (crypto.subtle).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");

// vault.js needs WebCrypto; Node >= 19 exposes it globally. Skip loudly otherwise.
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

test("stashPending encrypts + lists without touching the real vault", { skip: !HAS_CRYPTO }, async () => {
  const ok = await SoVault.stashPending("fronius", "owner@example.com", "hunter2-secret", "https://arrayoperator.com/");
  assert.equal(ok, true);
  // The real cred store must be untouched — nothing saved yet.
  assert.equal(await SoVault.get("fronius"), null);
  // Pending list surfaces vendor/username/origin, never the password.
  const pending = await SoVault.listPending();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].vendor, "fronius");
  assert.equal(pending[0].username, "owner@example.com");
  assert.equal(pending[0].origin, "https://arrayoperator.com/");
  assert.equal("password" in pending[0], false);
  // The raw storage record must not contain the plaintext password anywhere.
  assert.equal(JSON.stringify(_store.so_vault_pending).includes("hunter2-secret"), false);
});

test("stashPending rejects junk vendor codes and empty creds", { skip: !HAS_CRYPTO }, async () => {
  assert.equal(await SoVault.stashPending("evil-vendor", "u", "p", ""), false);
  assert.equal(await SoVault.stashPending("sma", "", "p", ""), false);
  assert.equal(await SoVault.stashPending("sma", "u", "", ""), false);
});

test("takePending decrypts once and removes the intent", { skip: !HAS_CRYPTO }, async () => {
  const rec = await SoVault.takePending("fronius");
  assert.deepEqual(rec, { username: "owner@example.com", password: "hunter2-secret" });
  // Consumed — a second take (a replayed confirm) gets nothing.
  assert.equal(await SoVault.takePending("fronius"), null);
  assert.equal((await SoVault.listPending()).length, 0);
});

test("dismissPending drops the intent without committing", { skip: !HAS_CRYPTO }, async () => {
  await SoVault.stashPending("gmp", "bruce@example.com", "gmp-pass", "https://nepooloperator.com/");
  assert.equal((await SoVault.listPending()).length, 1);
  assert.equal(await SoVault.dismissPending("gmp"), true);
  assert.equal((await SoVault.listPending()).length, 0);
  assert.equal(await SoVault.get("gmp"), null);   // never reached the vault
});

test("vault set/get round-trip still works (no regression)", { skip: !HAS_CRYPTO }, async () => {
  assert.equal(await SoVault.set("sma", "owner@example.com", "sma-pass"), true);
  assert.deepEqual(await SoVault.get("sma"), { username: "owner@example.com", password: "sma-pass" });
});
