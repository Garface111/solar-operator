"""Regenerate the public demo workbook at api/onboarding_dist/sample.xlsx.

This is the generic, customer-agnostic NEPOOL-GIS sample served by the FastAPI
static mount at /onboarding/sample.xlsx and linked from the marketing landing
page + onboarding welcome email.

Run locally:
    source venv/bin/activate && python -m scripts.regen_demo_workbook

NOTE: build_onboarding.sh does `rm -rf api/onboarding_dist`, which would wipe
this file. That script re-runs this regen at the end to put it back, so the
sample survives a frontend rebuild.
"""
from __future__ import annotations

import pathlib

from api.writers.demo_writer import build_demo_workbook

# api/onboarding_dist/sample.xlsx  (served at /onboarding/sample.xlsx)
DEST = pathlib.Path(__file__).resolve().parent.parent / "api" / "onboarding_dist" / "sample.xlsx"


def main() -> None:
    path = build_demo_workbook(DEST)
    size = path.stat().st_size
    print(f"✓ Wrote demo workbook: {path} ({size} bytes)")


if __name__ == "__main__":
    main()
