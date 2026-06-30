#!/usr/bin/env python3
"""Codegen: regenerate extension/smarthub_registry.js from the CSV catalog.

The Chrome content script can't import the Python registry, so this file is
GENERATED from api/data/providers/*.csv (the smarthub_host rows). Run after
editing any provider CSV. CI/validator checks it is up to date.

Usage:
    python scripts/gen_smarthub_registry_js.py          # write the file
    python scripts/gen_smarthub_registry_js.py --check   # exit 1 if stale
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.providers import PROVIDERS, SMARTHUB_HOSTS  # noqa: E402

OUT = ROOT / "extension" / "smarthub_registry.js"

_name_by_code = {p["code"]: p["label"] for p in PROVIDERS}


def render() -> str:
    # Deterministic order: by host.
    entries = sorted(
        ((host, code) for code, host in SMARTHUB_HOSTS.items()),
        key=lambda t: t[0],
    )
    lines = []
    for host, code in entries:
        name = _name_by_code.get(code, code)
        lines.append(f"    {json.dumps(host)}: {{")
        lines.append(f"      provider: {json.dumps(code)},")
        lines.append(f"      name: {json.dumps(name)},")
        lines.append("    },")
    body = "\n".join(lines)

    return f"""// smarthub_registry.js — GENERATED FILE. DO NOT EDIT BY HAND.
//
// Source of truth: api/data/providers/*.csv (rows with a smarthub_host).
// Regenerate:  python scripts/gen_smarthub_registry_js.py
// CI verifies this file is in sync via --check.
//
// Exposed on the global so it works in EVERY context that loads this file:
//   * content scripts + the popup (page world)  -> window.SMARTHUB_REGISTRY
//   * the background service worker (importScripts, no `window`) -> self.SMARTHUB_REGISTRY
// background.js importScripts() this for utility auto-login: it resolves a
// *.smarthub.coop login host to the right co-op CODE so the saved credential
// for that co-op is used.

(function (glob) {{
  "use strict";

  // Maps *.smarthub.coop hostname → lowercase provider code (matches DB)
  const SMARTHUB_REGISTRY = {{
{body}
  }};

  // Detect provider from the current page's hostname.
  // Unknown *.smarthub.coop hosts get a DETERMINISTIC discovered code
  // ("sh_<subdomain>") instead of masquerading as VEC — the backend mints
  // the identical code from user.hostname (api/adapters/smarthub.py
  // derive_provider_from_host), records the sighting, and alerts us to
  // promote the utility to the catalog. Data flows correctly on the very
  // first login from a brand-new co-op.
  function detectProvider(hostname) {{
    const host = hostname.toLowerCase();
    const entry = SMARTHUB_REGISTRY[host];
    if (entry) return entry;
    if (host.endsWith(".smarthub.coop")) {{
      const sub = host.slice(0, -".smarthub.coop".length);
      const code = sub.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 37);
      console.info(
        `[EnergyAgent] New SmartHub host: ${{host}} — capturing under ` +
          `discovered code sh_${{code}}. It will be promoted to the catalog automatically.`
      );
      return {{
        provider: "sh_" + code,
        name: sub.replace(/[-.]+/g, " ").replace(/\\b\\w/g, (c) => c.toUpperCase()) + " (SmartHub)",
        discovered: true,
      }};
    }}
    return null;
  }}

  // Resolve a *.smarthub.coop hostname to its co-op CODE (or null). Thin wrapper
  // over detectProvider used by the background service worker's utility
  // auto-login: given a SmartHub login URL's host, return the co-op code so the
  // matching saved credential (keyed by co-op code) is used. Covers known hosts
  // AND any new co-op via the deterministic sh_<subdomain> fallback.
  function codeForHost(hostname) {{
    const e = detectProvider(String(hostname || "").toLowerCase());
    return e ? e.provider : null;
  }}

  // Expose on the global of WHATEVER context loaded this file: `window` in the
  // page/content-script/popup world, `self`/`globalThis` in the service worker
  // (which importScripts() this — there is no `window` there).
  glob.SMARTHUB_REGISTRY = SMARTHUB_REGISTRY;
  glob.detectSmartHubProvider = detectProvider;
  glob.smartHubCodeForHost = codeForHost;
}})(typeof self !== "undefined" ? self : (typeof window !== "undefined" ? window : globalThis));
"""


def main() -> int:
    rendered = render()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != rendered:
            print(
                "STALE: extension/smarthub_registry.js is out of sync with the "
                "provider CSVs. Run: python scripts/gen_smarthub_registry_js.py",
                file=sys.stderr,
            )
            return 1
        print("ok: smarthub_registry.js is in sync")
        return 0
    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(SMARTHUB_HOSTS)} hosts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
