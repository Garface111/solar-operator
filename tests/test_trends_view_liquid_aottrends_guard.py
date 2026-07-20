"""Regression: trends-view-liquid.js must not throw when AOTrends is missing.

Sentry PYTHON-FASTAPI-1G — bingbot (and any load where trends-core.js never
assigns window.AOTrends) hit trends-view-liquid.js line 10 which called
C.registerView with C === undefined. trends-view-monthly.js and
trends-view-bars.js already guard with `if (!C || !C.registerView) return`
(or equivalent); liquid must match.

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
    Path(os.environ["AO_ROOT"]) / "public" / "trends-view-liquid.js"
    if os.environ.get("AO_ROOT")
    else None,
    Path("/root/array-operator/public/trends-view-liquid.js"),
    Path(__file__).resolve().parents[2] / "array-operator" / "public" / "trends-view-liquid.js",
    Path.home() / "array-operator" / "public" / "trends-view-liquid.js",
]


def _liquid_path() -> Path:
    for p in _CANDIDATES:
        if p and p.is_file():
            return p
    pytest.skip("array-operator/public/trends-view-liquid.js not found on this machine")


_NODE_HARNESS = r"""
const fs = require("fs");
const path = process.argv[1];
const mode = process.argv[2]; // "absent" | "present"
const code = fs.readFileSync(path, "utf8");

global.window = global;
global.document = {
  documentElement: { classList: { contains() { return false; } } },
  createElement() {
    return {
      style: {},
      classList: { add() {}, remove() {} },
      appendChild() {},
      addEventListener() {},
      removeEventListener() {},
    };
  },
};
global.matchMedia = () => ({ matches: false, addListener() {}, addEventListener() {} });

let registered = null;
if (mode === "present") {
  global.AOTrends = global.window.AOTrends = {
    registerView(key, def) { registered = { key, def }; },
    createCanvas() {
      return {
        canvas: { addEventListener() {}, removeEventListener() {} },
        start() {},
        stop() {},
      };
    },
    yearColor() { return "#3fd68a"; },
    MONTHS3: ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
  };
} else {
  delete global.AOTrends;
  delete global.window.AOTrends;
}

try {
  // eslint-disable-next-line no-eval
  eval(code);
} catch (e) {
  console.error("THROW:" + e.name + ":" + e.message);
  process.exit(2);
}

if (mode === "absent") {
  // Must load cleanly without AOTrends (no TypeError on registerView).
  console.log("OK_ABSENT");
  process.exit(0);
}
if (!registered || registered.key !== "liquid") {
  console.error("THROW:AssertionError: expected registerView('liquid') when AOTrends present");
  process.exit(3);
}
console.log("OK_PRESENT");
process.exit(0);
"""


def _run_node(mode: str) -> subprocess.CompletedProcess:
    liquid = _liquid_path()
    return subprocess.run(
        ["node", "-e", _NODE_HARNESS, str(liquid), mode],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_liquid_view_loads_without_aottrends():
    """C.registerView must not TypeError when window.AOTrends is missing."""
    r = _run_node("absent")
    assert r.returncode == 0, (
        f"trends-view-liquid.js threw without AOTrends:\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "OK_ABSENT" in r.stdout
    assert "registerView" not in (r.stderr or "")


def test_liquid_view_registers_when_aottrends_present():
    """When AOTrends exists, liquid still registers itself on the registry."""
    r = _run_node("present")
    assert r.returncode == 0, (
        f"trends-view-liquid.js failed with AOTrends present:\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "OK_PRESENT" in r.stdout
