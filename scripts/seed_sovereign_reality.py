#!/usr/bin/env python3
"""Seed docs/sovereign/reality/CHANGELOG.jsonl from git history.

Usage:
  .venv/bin/python scripts/seed_sovereign_reality.py
  .venv/bin/python scripts/seed_sovereign_reality.py --since 2026-04-01 --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.energy_agent_sovereign_reality import (  # noqa: E402
    regenerate_index,
    seed_from_git,
    status,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Seed Sovereign Reality File from git")
    p.add_argument("--since", default="2026-05-01")
    p.add_argument("--force", action="store_true")
    p.add_argument("--max-per-repo", type=int, default=800)
    args = p.parse_args()
    print("status before:", status())
    out = seed_from_git(since=args.since, max_per_repo=args.max_per_repo, force=args.force)
    print(json_dumps(out))
    regenerate_index()
    print("status after:", status())
    return 0 if out.get("ok") else 1


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, indent=2, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
