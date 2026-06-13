"""Canonical V2 REC-bearing fuel set + normalizer.

Single source of truth for the fuels an Array may represent. Mirrors the
frontend pickers (web/onboarding/src/lib/fuel.ts and web/app/src/lib/fuel.ts)
and the WRITERS keys in api/writers/registry.py, so the onboarding wizard, the
dashboard array CRUD, and the report dispatcher all validate against the SAME
list. A stale or garbage value can never reach the DB or break report dispatch
— it degrades to solar, leaving the sacred solar (GMCS) path untouched.
"""
from __future__ import annotations

from typing import Optional

DEFAULT_FUEL = "solar"
ALLOWED_FUELS = frozenset({"solar", "wind", "hydro", "digester", "storage"})


def normalize_fuel(value: Optional[str], fallback: str = DEFAULT_FUEL) -> str:
    """Return a valid fuel string.

    `value` wins if it's a known fuel; otherwise we fall back to `fallback`
    (itself validated), and ultimately to solar. Case/whitespace-insensitive.
    """
    v = (value or "").strip().lower()
    if v in ALLOWED_FUELS:
        return v
    fb = (fallback or "").strip().lower()
    return fb if fb in ALLOWED_FUELS else DEFAULT_FUEL
