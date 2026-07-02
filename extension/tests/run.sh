#!/usr/bin/env bash
# Extension capture-parser regression harness.
# Runs every *.test.js under extension/tests/ with Node's built-in test runner.
# No dependencies, no browser -- exercises the PURE parser helpers each content
# script exports via its browser-inert module.exports test hook.
#
# Usage:  bash extension/tests/run.sh   (exit 0 = all green)
set -euo pipefail

NODE="${NODE:-/usr/local/bin/node}"
if ! command -v "$NODE" >/dev/null 2>&1; then NODE="node"; fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== extension capture-parser tests =="
echo "node: $("$NODE" --version)"
echo

# node --test discovers and runs all matching files; TAP summary at the end.
exec "$NODE" --test "$DIR"/*.test.js
