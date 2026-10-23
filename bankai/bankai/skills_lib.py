"""The copilot's skills library: durable operating manuals it can consult.

Each skill is a markdown file in skills_library/ whose first lines are a
two-line "WHEN TO USE" header, e.g.

    WHEN TO USE: Before any call aimed at lowering a recurring bill.
    Also use when a promo rate just expired.

    # Bill negotiation ...

list_skills() returns only those headers, cheap enough to hand the agent every
turn; read_skill() returns the full text on demand. No database involved — the
library is code-shipped knowledge, not household data.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills_library"

_WHEN_TO_USE = re.compile(r"^\s*(?:[#>*\-\s]*)WHEN TO USE\s*:?\s*(.*)$", re.IGNORECASE)
_HEADER_SCAN_LINES = 6
_MARKUP = re.compile(r"^[#>*\-\s]+")


def _skill_path(name: str) -> Path:
    """Resolve a skill name to its file, refusing anything path-like."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"unknown skill {name!r} — call list_skills for valid names")
    return SKILLS_DIR / f"{name}.md"


def _parse_when_to_use(text: str) -> str:
    """Pull the 2-line WHEN TO USE header out of a skill's raw markdown."""
    lines = text.splitlines()
    for index, line in enumerate(lines[:_HEADER_SCAN_LINES]):
        match = _WHEN_TO_USE.match(line)
        if not match:
            continue
        parts = [match.group(1).strip()]
        for follow in lines[index + 1 : index + 2]:
            stripped = follow.strip()
            if not stripped or stripped.startswith("#"):
                break
            parts.append(_MARKUP.sub("", stripped).strip())
        return " ".join(p for p in parts if p)
    return ""


def _title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _skill_names() -> tuple[str, ...]:
    """Skill names on disk, read fresh so a newly dropped-in pack shows up."""
    if not SKILLS_DIR.is_dir():
        return ()
    return tuple(sorted(p.stem for p in SKILLS_DIR.glob("*.md")))


def list_skills() -> list[dict]:
    """Every skill with its indexable WHEN TO USE header (no bodies)."""
    out: list[dict] = []
    for name in _skill_names():
        text = _skill_path(name).read_text(encoding="utf-8")
        out.append(
            {
                "name": name,
                "title": _title(text),
                "when_to_use": _parse_when_to_use(text),
                "words": len(text.split()),
            }
        )
    return out


def read_skill(name: str) -> str:
    """Full markdown text of one skill. ValueError if there is no such skill."""
    path = _skill_path(name)
    if not path.is_file():
        known = ", ".join(_skill_names()) or "(none installed)"
        raise ValueError(f"unknown skill {name!r} — available: {known}")
    return path.read_text(encoding="utf-8")
