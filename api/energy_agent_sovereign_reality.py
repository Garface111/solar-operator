"""Sovereign Reality File — cold hard truth of Array Operator product changes.

Ford 2026-07-22: Sovereign needs a single chronological memory of every FE/BE
change to Array Operator. It reads this on every cortex wake and appends every
change it (or Ford/agents) ships. Repo git is still available; this file is the
mind's *reasoned* timeline — short summaries, surfaces, why it mattered.

Layout (repo-relative, committed when possible):
  docs/sovereign/reality/README.md
  docs/sovereign/reality/CHANGELOG.jsonl   # append-only cold truth
  docs/sovereign/reality/INDEX.md         # rolling human summary (regenerated)

Prompt budget: wake gets INDEX + last N entries (not the whole file).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("energy_agent.sovereign.reality")

# Prefer explicit SOVEREIGN_REPO_ROOT, else package parent (solar-operator)
_PKG = Path(__file__).resolve().parent
_DEFAULT_SO_ROOT = _PKG.parent  # .../solar-operator
SO_ROOT = Path(os.getenv("SOVEREIGN_REPO_ROOT") or _DEFAULT_SO_ROOT).resolve()
AO_ROOT = Path(
    os.getenv("ARRAY_OPERATOR_ROOT")
    or os.getenv("SOVEREIGN_AO_ROOT")
    or (SO_ROOT.parent / "array-operator")
).resolve()

REALITY_DIR = SO_ROOT / "docs" / "sovereign" / "reality"
CHANGELOG_PATH = REALITY_DIR / "CHANGELOG.jsonl"
INDEX_PATH = REALITY_DIR / "INDEX.md"
README_PATH = REALITY_DIR / "README.md"

# How many full entries the cortex sees each wake
WAKE_TAIL = int(os.getenv("SOVEREIGN_REALITY_WAKE_TAIL", "60"))
# Max chars injected into the think prompt for reality block
WAKE_BUDGET_CHARS = int(os.getenv("SOVEREIGN_REALITY_WAKE_CHARS", "14000"))


def _rel_path(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(SO_ROOT.resolve()))
    except Exception:
        return str(p)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utcnow()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def ensure_dir() -> Path:
    REALITY_DIR.mkdir(parents=True, exist_ok=True)
    if not README_PATH.exists():
        README_PATH.write_text(
            "# Sovereign Reality File\n\n"
            "Cold hard truth of Array Operator product changes.\n\n"
            "- `CHANGELOG.jsonl` — append-only. One JSON object per line.\n"
            "- `INDEX.md` — regenerated summary for cortex wake.\n\n"
            "Sovereign reads this on every cortex cycle. Every ship appends.\n",
            encoding="utf-8",
        )
    if not CHANGELOG_PATH.exists():
        CHANGELOG_PATH.write_text("", encoding="utf-8")
    return REALITY_DIR


def classify_surfaces(files: list[str]) -> list[str]:
    """frontend | backend | extension | docs | ops | other"""
    surfaces: set[str] = set()
    for f in files or []:
        fl = f.replace("\\", "/").lower()
        if fl.startswith("public/") or fl.endswith((".js", ".css", ".html")) and "public" in fl:
            surfaces.add("frontend")
        elif fl.startswith("api/") or fl.endswith(".py"):
            surfaces.add("backend")
        elif "extension/" in fl or fl.startswith("extension"):
            surfaces.add("extension")
        elif fl.startswith("docs/") or fl.endswith(".md"):
            surfaces.add("docs")
        elif any(x in fl for x in ("scheduler", "worker", "railway", "netlify", "scripts/")):
            surfaces.add("ops")
        else:
            surfaces.add("other")
    return sorted(surfaces) or ["other"]


def append_entry(
    *,
    summary: str,
    source: str = "sovereign",
    repos: list[str] | None = None,
    files: list[str] | None = None,
    surfaces: list[str] | None = None,
    sha: str | None = None,
    author: str | None = None,
    job_id: str | None = None,
    why: str | None = None,
    ts: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one reality line. Idempotent-ish: skip exact sha+repo duplicates."""
    ensure_dir()
    files = list(files or [])[:80]
    repos = list(repos or [])
    entry: dict[str, Any] = {
        "ts": ts or _iso(),
        "source": (source or "unknown")[:40],
        "repos": repos,
        "summary": (summary or "").strip()[:500],
        "files": files,
        "surfaces": surfaces or classify_surfaces(files),
    }
    if sha:
        entry["sha"] = sha[:40]
    if author:
        entry["author"] = author[:120]
    if job_id:
        entry["job_id"] = job_id[:80]
    if why:
        entry["why"] = why[:600]
    if extra:
        for k, v in extra.items():
            if k not in entry and v is not None:
                entry[k] = v

    if sha and repos:
        # de-dupe by sha across file
        try:
            for line in CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()[-500:]:
                if not line.strip():
                    continue
                try:
                    prev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if prev.get("sha") == entry.get("sha") and set(prev.get("repos") or []) & set(repos):
                    return {"ok": True, "skipped": "duplicate_sha", "entry": prev}
        except OSError:
            pass

    with CHANGELOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    try:
        regenerate_index()
    except Exception as e:  # noqa: BLE001
        log.warning("reality index regen failed: %s", e)
    return {"ok": True, "entry": entry, "path": str(CHANGELOG_PATH)}


def read_entries(*, limit: int | None = None, offset: int = 0) -> list[dict]:
    ensure_dir()
    if not CHANGELOG_PATH.exists():
        return []
    lines = [
        ln for ln in CHANGELOG_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    if offset:
        lines = lines[offset:]
    if limit is not None:
        lines = lines[-limit:] if offset == 0 and limit > 0 else lines[:limit]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def entry_count() -> int:
    ensure_dir()
    if not CHANGELOG_PATH.exists():
        return 0
    n = 0
    with CHANGELOG_PATH.open(encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                n += 1
    return n


def regenerate_index() -> str:
    """Human + model readable rolling summary of the reality file."""
    ensure_dir()
    entries = read_entries()
    total = len(entries)
    by_source: dict[str, int] = {}
    by_surface: dict[str, int] = {}
    by_repo: dict[str, int] = {}
    for e in entries:
        by_source[e.get("source") or "?"] = by_source.get(e.get("source") or "?", 0) + 1
        for s in e.get("surfaces") or []:
            by_surface[s] = by_surface.get(s, 0) + 1
        for r in e.get("repos") or []:
            by_repo[r] = by_repo.get(r, 0) + 1

    # Last 14 days cluster by day
    recent = entries[-80:]
    lines = [
        "# Reality INDEX — Array Operator cold truth",
        "",
        f"_Regenerated {_iso()}_",
        "",
        f"**Total changes recorded:** {total}",
        "",
        "## By source",
    ]
    for k, v in sorted(by_source.items(), key=lambda x: -x[1]):
        lines.append(f"- `{k}`: {v}")
    lines += ["", "## By surface"]
    for k, v in sorted(by_surface.items(), key=lambda x: -x[1]):
        lines.append(f"- `{k}`: {v}")
    lines += ["", "## By repo"]
    for k, v in sorted(by_repo.items(), key=lambda x: -x[1]):
        lines.append(f"- `{k}`: {v}")
    lines += [
        "",
        "## How Sovereign uses this",
        "",
        "1. On every **cortex wake**, load this INDEX + the last "
        f"{WAKE_TAIL} CHANGELOG lines.",
        "2. After every **ship** (code job, feature ship, Ford-logged change), "
        "**append** one JSONL line — never rewrite history.",
        "3. Prefer this timeline over inventing product history. Git remains "
        "the raw audit; this is the reasoned product memory.",
        "",
        "## Latest entries (tail)",
        "",
    ]
    for e in recent[-25:]:
        ts = (e.get("ts") or "")[:19]
        repos = ",".join(e.get("repos") or []) or "?"
        surf = ",".join(e.get("surfaces") or [])
        lines.append(
            f"- `{ts}` [{e.get('source')}] ({repos}/{surf}) {e.get('summary')}"
        )
    text = "\n".join(lines) + "\n"
    INDEX_PATH.write_text(text, encoding="utf-8")
    return text


def load_for_wake(*, tail: int | None = None) -> dict[str, Any]:
    """Payload injected into cortex prompt — budgeted."""
    ensure_dir()
    tail = tail if tail is not None else WAKE_TAIL
    entries = read_entries(limit=tail)
    index = ""
    if INDEX_PATH.exists():
        index = INDEX_PATH.read_text(encoding="utf-8")
    else:
        index = regenerate_index()
    # Trim index if huge
    if len(index) > 6000:
        index = index[:6000] + "\n…[INDEX truncated]\n"
    compact = []
    for e in entries:
        compact.append(
            {
                "ts": e.get("ts"),
                "source": e.get("source"),
                "repos": e.get("repos"),
                "surfaces": e.get("surfaces"),
                "summary": e.get("summary"),
                "sha": e.get("sha"),
                "job_id": e.get("job_id"),
                "why": e.get("why"),
            }
        )
    payload = {
        "doctrine": (
            "REALITY FILE is cold hard truth of Array Operator product changes "
            "(frontend + backend + ops). Read it before inventing history. "
            "When YOU ship anything, append via the ship path (automatic) or "
            "reality_record action. Never rewrite past lines."
        ),
        "path": _rel_path(CHANGELOG_PATH),
        "total_entries": entry_count(),
        "index_md": index,
        "recent_changes": compact,
    }
    raw = json.dumps(payload, default=str)
    if len(raw) > WAKE_BUDGET_CHARS:
        # drop oldest from recent until under budget
        while len(compact) > 10 and len(json.dumps(payload, default=str)) > WAKE_BUDGET_CHARS:
            compact.pop(0)
            payload["recent_changes"] = compact
        payload["truncated"] = True
    return payload


def record_ship(
    *,
    title: str,
    repo: str,
    job_id: str | None = None,
    files: list[str] | None = None,
    sha: str | None = None,
    brief: str | None = None,
    source: str = "sovereign",
) -> dict[str, Any]:
    """Called after a code job ships (prod or sandbox)."""
    summary = (title or "untitled change").strip()
    if brief and len(summary) < 80:
        first = re.split(r"[\n\r]", brief.strip())[0][:200]
        if first and first.lower() not in summary.lower():
            summary = f"{summary} — {first}"
    return append_entry(
        summary=summary[:500],
        source=source,
        repos=[repo] if repo else [],
        files=files or [],
        sha=sha,
        job_id=job_id,
        author="Sovereign" if source == "sovereign" else source,
        why=(brief or "")[:600] or None,
    )


def _git_log(
    repo: Path,
    *,
    since: str | None = None,
    max_count: int = 500,
    paths: list[str] | None = None,
) -> list[dict]:
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        # worktree
        if not (repo / ".git").exists():
            return []
    cmd = [
        "git", "-C", str(repo), "log",
        f"--max-count={max_count}",
        "--date=iso-strict",
        "--pretty=format:%H%x09%ad%x09%an%x09%s",
        "--name-only",
    ]
    if since:
        cmd.append(f"--since={since}")
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        log.warning("git log failed %s: %s", repo, e)
        return []
    if r.returncode != 0:
        log.warning("git log rc=%s: %s", r.returncode, (r.stderr or "")[:300])
        return []
    commits: list[dict] = []
    cur: dict | None = None
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        if "\t" in line and re.match(r"^[0-9a-f]{7,40}\t", line):
            if cur:
                commits.append(cur)
            parts = line.split("\t", 3)
            cur = {
                "sha": parts[0],
                "ts": parts[1] if len(parts) > 1 else None,
                "author": parts[2] if len(parts) > 2 else None,
                "summary": parts[3] if len(parts) > 3 else "",
                "files": [],
            }
        elif cur is not None:
            cur["files"].append(line.strip())
    if cur:
        commits.append(cur)
    return commits


def seed_from_git(
    *,
    since: str = "2026-05-01",
    max_per_repo: int = 800,
    force: bool = False,
) -> dict[str, Any]:
    """Bootstrap CHANGELOG from git history of AO + SO product paths.

    Skips if file already has entries unless force=True (then appends only
    missing shas).
    """
    ensure_dir()
    existing_shas: set[str] = set()
    if CHANGELOG_PATH.exists() and not force:
        for e in read_entries():
            if e.get("sha"):
                existing_shas.add(e["sha"])
        if existing_shas and entry_count() > 20:
            return {
                "ok": True,
                "skipped": "already_seeded",
                "count": entry_count(),
            }

    added = 0
    # array-operator — whole public product surface
    if AO_ROOT.exists():
        for c in reversed(_git_log(AO_ROOT, since=since, max_count=max_per_repo)):
            if c["sha"] in existing_shas:
                continue
            files = c.get("files") or []
            append_entry(
                summary=c.get("summary") or "(no subject)",
                source=_author_source(c.get("author") or ""),
                repos=["array-operator"],
                files=files[:60],
                surfaces=classify_surfaces(files),
                sha=c["sha"],
                author=c.get("author"),
                ts=_normalize_ts(c.get("ts")),
            )
            existing_shas.add(c["sha"])
            added += 1

    # solar-operator — backend that powers AO (api/, extension/, relevant docs)
    if SO_ROOT.exists():
        for c in reversed(
            _git_log(
                SO_ROOT,
                since=since,
                max_count=max_per_repo,
                paths=["api/", "extension/", "docs/plans/", "docs/sovereign/"],
            )
        ):
            if c["sha"] in existing_shas:
                continue
            files = c.get("files") or []
            # skip pure nepool-only noise if no AO-ish paths
            ao_ish = any(
                p.startswith(("api/energy_agent", "api/array_owners", "api/inverter",
                              "api/energy_agent_sovereign", "extension/", "docs/sovereign",
                              "api/pricing", "api/account", "api/onboarding"))
                or "array" in p.lower() or "sovereign" in p.lower()
                for p in files
            )
            if not ao_ish and files:
                # still keep if commit message mentions array/energy/sovereign
                subj = (c.get("summary") or "").lower()
                if not any(w in subj for w in ("array", "energy", "sovereign", "fleet", "ao ", "owner")):
                    continue
            append_entry(
                summary=c.get("summary") or "(no subject)",
                source=_author_source(c.get("author") or ""),
                repos=["solar-operator"],
                files=files[:60],
                surfaces=classify_surfaces(files),
                sha=c["sha"],
                author=c.get("author"),
                ts=_normalize_ts(c.get("ts")),
            )
            existing_shas.add(c["sha"])
            added += 1

    regenerate_index()
    return {"ok": True, "added": added, "total": entry_count(), "path": str(CHANGELOG_PATH)}


def _author_source(author: str) -> str:
    a = (author or "").lower()
    if "sovereign" in a:
        return "sovereign"
    if "claude" in a or "noreply@anthropic" in a:
        return "agent"
    if "ford" in a or "garface" in a:
        return "ford"
    if "dependabot" in a or "autofix" in a:
        return "bot"
    return "git"


def _normalize_ts(ts: str | None) -> str:
    if not ts:
        return _iso()
    try:
        # git iso-strict
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d.isoformat()
    except ValueError:
        return ts


def status() -> dict[str, Any]:
    ensure_dir()
    return {
        "path": str(CHANGELOG_PATH),
        "index_path": str(INDEX_PATH),
        "total": entry_count(),
        "wake_tail": WAKE_TAIL,
        "so_root": str(SO_ROOT),
        "ao_root": str(AO_ROOT),
        "ao_exists": AO_ROOT.exists(),
    }
