"""Regression: command-center.js must not throw ReferenceError: FleetStore is not defined.

Sentry PYTHON-FASTAPI-12 — bingbot (and any load where fleet-store.js never
assigns window.FleetStore) hit command-center.js's IIFE bottom which called
bare FleetStore.subscribe / FleetStore.load. sandbox.js already guards with
`if (window.FleetStore)`; command-center must match.

The AO frontend lives in the sibling array-operator repo; this test loads that
file under a minimal DOM shim via node.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Common checkout locations for the Array Operator frontend.
_CANDIDATES = [
    Path(os.environ["AO_ROOT"]) / "public" / "command-center.js"
    if os.environ.get("AO_ROOT")
    else None,
    Path("/root/array-operator/public/command-center.js"),
    Path(__file__).resolve().parents[2] / "array-operator" / "public" / "command-center.js",
    Path.home() / "array-operator" / "public" / "command-center.js",
]


def _command_center_path() -> Path:
    for p in _CANDIDATES:
        if p and p.is_file():
            return p
    pytest.skip("array-operator/public/command-center.js not found on this machine")


_NODE_HARNESS = r"""
const fs = require("fs");
const path = process.argv[1];
const mode = process.argv[2]; // "absent" | "present"
const code = fs.readFileSync(path, "utf8");

global.window = global;
global.location = { hash: "#arrays", href: "https://arrayoperator.com/#arrays", search: "" };
global.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
global.navigator = { userAgent: "bingbot/2.0" };
global.document = {
  readyState: "complete",
  addEventListener() {},
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() {
    return {
      style: {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      setAttribute() {},
      appendChild() {},
    };
  },
};

let subscribed = false, loaded = false;
if (mode === "present") {
  global.FleetStore = global.window.FleetStore = {
    subscribe() { subscribed = true; },
    load() { loaded = true; },
    snapshot() { return { arrays: [] }; },
    isLoaded() { return true; },
    isSimulated() { return false; },
    lastUpdate() { return 0; },
    energyRate() { return 0.21; },
    REC_PER_MWH: 38,
    WINDOW_DAYS: 14,
  };
} else {
  // Explicitly no FleetStore — the production bug path (crawler / missing script).
  delete global.FleetStore;
  delete global.window.FleetStore;
}

try {
  // eslint-disable-next-line no-eval
  eval(code);
} catch (e) {
  console.error("THROW:" + e.name + ":" + e.message);
  process.exit(2);
}

if (mode === "absent") {
  // Must load cleanly without a store (no ReferenceError).
  console.log("OK_ABSENT");
  process.exit(0);
}
if (!subscribed || !loaded) {
  console.error("THROW:AssertionError: expected subscribe+load when store present");
  process.exit(3);
}
console.log("OK_PRESENT");
process.exit(0);
"""


def _run_node(mode: str) -> subprocess.CompletedProcess:
    cc = _command_center_path()
    return subprocess.run(
        ["node", "-e", _NODE_HARNESS, str(cc), mode],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_command_center_loads_without_fleetstore():
    """Bare FleetStore at IIFE end must not ReferenceError when store is missing."""
    r = _run_node("absent")
    assert r.returncode == 0, (
        f"command-center.js threw without FleetStore:\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "OK_ABSENT" in r.stdout
    assert "FleetStore is not defined" not in (r.stdout + r.stderr)


def test_command_center_wires_store_when_present():
    """When FleetStore exists, init still subscribes and loads."""
    r = _run_node("present")
    assert r.returncode == 0, (
        f"command-center.js failed with FleetStore present:\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "OK_PRESENT" in r.stdout
