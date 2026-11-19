"""Self-improvement: the copilot reads its own code and proposes real patches.

The loop, and where each step's trust lives:

1. READ — read_source lets the copilot inspect its own code (its package and
   tests only; never .env, the database, or the vault). Read-only.
2. PROPOSE — propose_patch records full new file contents plus rationale and the
   tests that prove it. This is DATA: nothing runs, nothing ships. A diff
   against the deployed source is computed with difflib — no git, no execution.
3. EVALUATE — the proposal's tests run in a CREDENTIAL-FREE, NETWORK-CUT sandbox
   (agent-authored tests are still arbitrary code, so they run as an
   unprivileged user with no network and no access to the live secrets or DB).
   Until a sandbox is configured, evaluation is refused, not faked.
4. SHIP — a human merges. There is no path from a proposal to production that
   the copilot can walk alone. That is the whole safety model: capability to
   PROPOSE is unbounded; capability to DEPLOY stays with Ford.

The path guard is the load-bearing security control for READ and PROPOSE:
touching anything outside the package/tests — above all .env and *.db — is
refused, so neither a genuine mistake nor a subverted turn can read secrets out
or write them into a "patch".
"""
from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import CodeProposal

log = logging.getLogger("bankai.selfimprove")

#: Only the copilot's own package and its tests are in scope for reading or
#: patching. Everything else — .env, the database, the document vault, the
#: node bridge, systemd units — is off limits by construction.
ALLOWED_PREFIXES = ("bankai/", "tests/")
MAX_FILES_PER_PROPOSAL = 20
MAX_FILE_BYTES = 200_000


class PathRefused(ValueError):
    """A path outside the self-improvement surface."""


def safe_repo_path(rel: str) -> str:
    """Validate a repo-relative path or raise. The one security gate for both
    reading source and applying a patch."""
    rel = (rel or "").strip().replace("\\", "/")
    if not rel:
        raise PathRefused("empty path")
    if rel.startswith("/") or ".." in rel.split("/"):
        raise PathRefused(f"path escapes the repo: {rel!r}")
    low = rel.lower()
    if low.endswith(".env") or ".env" in Path(low).name or low.endswith((".db", ".sqlite")):
        raise PathRefused(f"refused — secrets/data are never in scope: {rel!r}")
    if not any(rel.startswith(p) for p in ALLOWED_PREFIXES):
        raise PathRefused(
            f"refused — only {', '.join(ALLOWED_PREFIXES)} are in scope: {rel!r}"
        )
    return rel


def _source_root() -> Path:
    """The deployed tree the copilot actually runs from — config.BASE_DIR is the
    package parent (…/bankai for the package, with tests/ beside it)."""
    return Path(config.BASE_DIR)


def read_source(rel: str, max_bytes: int = MAX_FILE_BYTES) -> dict:
    """Read one of the copilot's own source files, path-guarded."""
    rel = safe_repo_path(rel)
    path = _source_root() / rel
    if not path.is_file():
        return {"error": f"no such source file: {rel}"}
    data = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(data.encode("utf-8", "replace")) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return {"path": rel, "content": data, "truncated": truncated,
            "lines": data.count("\n") + 1}


def list_source(subdir: str = "bankai") -> dict:
    """List the source files available to read/patch under a package subdir."""
    root = _source_root()
    try:
        base_rel = safe_repo_path(subdir.rstrip("/") + "/_probe")
    except PathRefused:
        return {"error": f"{subdir!r} is not an in-scope directory"}
    base = (root / subdir).resolve()
    files = []
    for p in sorted(base.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        try:
            safe_repo_path(rel)
        except PathRefused:
            continue
        files.append(rel)
    return {"subdir": subdir, "files": files}


def compute_diff(files: dict[str, str]) -> str:
    """Unified diff of a proposed file set against the deployed source — pure
    difflib, so review needs neither git nor a checkout."""
    root = _source_root()
    chunks: list[str] = []
    for rel, new in files.items():
        current = ""
        path = root / rel
        if path.is_file():
            current = path.read_text(encoding="utf-8", errors="replace")
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
        text = "".join(diff)
        chunks.append(text if text else f"--- {rel}: (no change)\n")
    return "\n".join(chunks)


def record_proposal(
    session: Session,
    *,
    title: str,
    rationale: str,
    files: dict[str, str],
    test_paths: str = "",
    proposed_by: str = "copilot",
) -> CodeProposal:
    """Validate and store a patch. Raises PathRefused/ValueError on bad input;
    nothing is executed."""
    if not title.strip():
        raise ValueError("title is required")
    if not files:
        raise ValueError("a proposal must change at least one file")
    if len(files) > MAX_FILES_PER_PROPOSAL:
        raise ValueError(f"too many files ({len(files)} > {MAX_FILES_PER_PROPOSAL})")
    clean: dict[str, str] = {}
    for rel, content in files.items():
        rel = safe_repo_path(rel)
        if not isinstance(content, str):
            raise ValueError(f"file {rel} content must be a string")
        if len(content.encode("utf-8", "replace")) > MAX_FILE_BYTES:
            raise ValueError(f"file {rel} is too large ({MAX_FILE_BYTES} byte cap)")
        clean[rel] = content
    for t in test_paths.replace(",", " ").split():
        safe_repo_path(t)  # tests must be in scope too

    row = CodeProposal(
        title=title.strip()[:200],
        rationale=rationale.strip(),
        files_json=json.dumps(clean),
        test_paths=test_paths.strip()[:400],
        diff=compute_diff(clean),
        proposed_by=proposed_by.strip()[:60] or "copilot",
        status="proposed",
    )
    session.add(row)
    session.flush()
    return row


def as_dicts(session: Session, include_closed: bool = False) -> list[dict]:
    query = select(CodeProposal).order_by(CodeProposal.created_at.desc())
    if not include_closed:
        query = query.where(CodeProposal.status.in_(
            ("proposed", "awaiting_sandbox", "passed", "failed")
        ))
    out = []
    for r in session.execute(query).scalars():
        files = json.loads(r.files_json or "{}")
        out.append({
            "id": r.id,
            "title": r.title,
            "rationale": r.rationale,
            "status": r.status,
            "files": sorted(files),
            "test_paths": r.test_paths,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "test_output_tail": (r.test_output or "")[-1200:],
        })
    return out


def files_of(proposal: CodeProposal) -> dict[str, str]:
    return json.loads(proposal.files_json or "{}")
