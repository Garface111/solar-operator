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
// Exported as window.SMARTHUB_REGISTRY so smarthub_content.js can read it
// without module imports (content scripts run in the page context).

(function () {{
  "use strict";

  // Maps *.smarthub.coop hostname → lowercase provider code (matches DB)
  const SMARTHUB_REGISTRY = {{
{body}
  }};

  // Detect provider from the current page's hostname.
  // Falls back to "vec" (the first deployed utility) for unknown *.smarthub.coop hosts.
  function detectProvider(hostname) {{
    const entry = SMARTHUB_REGISTRY[hostname.toLowerCase()];
    if (entry) return entry;
    if (hostname.endsWith(".smarthub.coop")) {{
      console.warn(
        `[Solar Operator] Unknown SmartHub host: ${{hostname}}. ` +
          "Treating as VEC (vermontelectric). Add this host to a provider CSV."
      );
      return {{ provider: "vec", name: "Unknown SmartHub Utility" }};
    }}
    return null;
  }}

  // Expose on window so smarthub_content.js (loaded in the same content-script
  // world) can call window.SMARTHUB_REGISTRY and window.detectSmartHubProvider.
  window.SMARTHUB_REGISTRY = SMARTHUB_REGISTRY;
  window.detectSmartHubProvider = detectProvider;
}})();
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
